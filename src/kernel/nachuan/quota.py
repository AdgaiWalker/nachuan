"""Typed quota snapshots: unknown is never zero; generations fence stale results."""
import asyncio
import copy
import time
from decimal import Decimal, InvalidOperation
from .credentials import atomic
from .errors import KernelError


def quantity(value):
    if not isinstance(value,str): raise ValueError('quota quantities must be decimal strings')
    try: number=Decimal(value)
    except InvalidOperation: raise ValueError('invalid quota quantity') from None
    if not number.is_finite() or number < 0: raise ValueError('invalid quota quantity')
    return number


def snapshot(value, uid, now, ttl):
    if not isinstance(value,dict) or value.get('accountId') != uid or not isinstance(value.get('dimensions'),list): raise ValueError('invalid quota snapshot')
    observed=value.get('observedAt')
    expiry=value.get('expiresAt')
    if type(observed) not in (int,float) or type(expiry) not in (int,float) or not now-86400 <= observed <= now+5 or not observed < expiry <= observed+ttl: raise ValueError('invalid quota timestamps')
    dimensions=[]
    ids=set()
    for source in value['dimensions']:
        if not isinstance(source,dict): raise ValueError('invalid quota dimension')
        kind=source.get('kind'); key=source.get('id')
        if not isinstance(key,str) or not key or key in ids or kind not in ('tokens','requests','points','seat','batches'): raise ValueError('invalid quota dimension')
        ids.add(key)
        d={'id':key,'kind':kind}
        if kind=='seat':
            if source.get('status') not in ('active','inactive','unknown'): raise ValueError('invalid seat')
            d['status']=source['status']
        else:
            if not isinstance(source.get('unit'),str) or not source['unit']: raise ValueError('quota unit required')
            d['unit']=source['unit']
            if kind=='batches':
                batches=[]; seen=set()
                for batch in source.get('batches',[]):
                    bid=batch.get('batchId'); exp=batch.get('expiresAt')
                    if not isinstance(bid,str) or bid in seen or type(exp) not in (int,float) or not 0<exp<float('inf'): raise ValueError('invalid batch')
                    seen.add(bid); quantity(batch.get('remaining'))
                    batches.append({'batchId':bid,'remaining':batch['remaining'],'expiresAt':exp})
                d['batches']=batches
                d['remaining']=str(sum((quantity(b['remaining']) for b in batches if b['expiresAt']>now),Decimal(0)))
            else:
                quantity(source.get('remaining')); d['remaining']=source['remaining']
                if 'limit' in source: quantity(source['limit']); d['limit']=source['limit']
            if 'resetAt' in source:
                reset=source['resetAt']
                if type(reset) not in (int,float) or not 0<reset<float('inf'): raise ValueError('invalid reset time')
                d['resetAt']=reset
        dimensions.append(d)
    return {'accountId':uid,'observedAt':observed,'expiresAt':expiry,'dimensions':dimensions}


class QuotaService:
    def __init__(self,kernel,path):
        self.kernel=kernel; self.path=path; self.records={}; self.pending={}
        if path.exists():
            import json
            try: self.records=json.loads(path.read_text())
            except (ValueError,OSError): self.records={}

    def get(self,plugin,uid):
        account=plugin.account_pool._accounts.get(uid) if plugin.account_pool else None
        if not account: raise KernelError(404,'account_not_found')
        from .credentials import read
        generation=read(account.credential_file)['generation']
        if not plugin.capabilities.get('quotaQuery') or not plugin.spec.quota.get('query'):
            return {'state':'unsupported','accountId':uid}
        record=copy.deepcopy(self.records.get(plugin.name+':'+uid,{}))
        if record.get('generation') != generation: return {'state':'unknown','accountId':uid}
        record['state']='stale' if record['snapshot']['expiresAt']<=time.time() else 'known'
        if record.get('error'): record['state']='error'
        for d in record['snapshot']['dimensions']:
            if d['kind']=='batches': d['remaining']=str(sum((quantity(b['remaining']) for b in d['batches'] if b['expiresAt']>time.time()),Decimal(0)))
        return record

    def exhausted(self,plugin,uid):
        if not plugin.spec.quota.get('reliableExhaustion'): return False
        record=self.get(plugin,uid)
        if record['state']!='known': return False
        for d in record['snapshot']['dimensions']:
            if d.get('resetAt',float('inf'))<=time.time(): continue
            if d['kind']=='seat' and d['status']=='inactive': return True
            if d['kind']!='seat' and quantity(d['remaining'])==0: return True
        return False

    def recovered(self,record):
        # Recovery needs affirmative current data, independent of admission policy.
        if record.get('state')!='known': return False
        dimensions=record['snapshot']['dimensions']
        if not dimensions: return False
        for d in dimensions:
            if d.get('resetAt',float('inf'))<=time.time(): return False
            if d['kind']=='seat':
                if d['status']!='active': return False
            elif quantity(d['remaining'])<=0: return False
        return True

    async def refresh(self,plugin,uid):
        # Key by plugin and account; callers share one query and do not cancel each other.
        key=plugin.name+':'+uid
        if key not in self.pending:
            task=asyncio.create_task(self._refresh(plugin,uid,key))
            self.pending[key]=task
            def finished(done):
                if self.pending.get(key) is done: self.pending.pop(key,None)
                if not done.cancelled(): done.exception()
            task.add_done_callback(finished)
        task=self.pending[key]
        try: return await asyncio.shield(task)
        finally:
            if task.done() and self.pending.get(key) is task: self.pending.pop(key,None)

    async def _refresh(self,plugin,uid,key):
        if self.get(plugin,uid)['state']=='unsupported': raise KernelError(501,'unsupported')
        from .credentials import read
        async with self.kernel.maintenance(plugin,uid) as account:
            generation=read(account.credential_file)['generation']
            try:
                value=await plugin.transport.call('quota.query',{'credential':{'id':uid,'secret':account.credential()}},min(self.kernel.config.request_timeout_seconds,30))
                data=snapshot(value,uid,time.time(),plugin.spec.quota.get('ttlSeconds',300))
                if generation != read(account.credential_file)['generation']: raise KernelError(409,'credential_generation_conflict')
                self.records[key]={'generation':generation,'snapshot':data}
                atomic(self.path,self.records)
            except KernelError: raise
            except Exception:
                if key in self.records:
                    self.records[key]['error']='quota_query_failed'; atomic(self.path,self.records)
                raise KernelError(502,'quota_query_failed') from None
        record=self.get(plugin,uid)
        from .accounts import AccountState
        if account.enabled and account.state==AccountState.EXHAUSTED and self.recovered(record):
            account.state=AccountState.READY; account.until=0; account.reason=''
            plugin.account_pool._dirty=True
        return record

    async def close(self):
        for task in self.pending.values(): task.cancel()
        await asyncio.gather(*self.pending.values(),return_exceptions=True)
        self.pending.clear()
