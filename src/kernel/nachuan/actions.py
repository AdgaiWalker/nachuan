"""Persistent, bounded action runs. Side effects are never automatically replayed."""
import asyncio
import hashlib
import json
import time
import uuid
from .credentials import atomic, read
from .errors import KernelError
from .schema import validate, protect, OBJECT

TERMINAL={'succeeded','failed','cancelled','unknown'}


class ActionRunner:
    def __init__(self,kernel,path,limit=500):
        self.kernel=kernel; self.path=path; self.limit=limit
        self.runs={}; self.tasks={}; self.keys=set()
        self.history_error=None
        self.marker=path.with_suffix('.initialized')
        try:
            loaded=json.loads(path.read_text())
            if not isinstance(loaded,dict): raise ValueError('invalid action history')
            for rid,run in loaded.items():
                if not isinstance(run,dict) or run.get('id')!=rid or not isinstance(rid,str): raise ValueError('invalid run identity')
                if not all(isinstance(run.get(k),str) for k in ('plugin','action','fingerprint')): raise ValueError('invalid run metadata')
                if run.get('state') not in TERMINAL|{'queued','running'} or type(run.get('sideEffect')) is not bool: raise ValueError('invalid run state')
                digest=run.get('idempotencyHash')
                if digest is not None and (not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)): raise ValueError('invalid idempotency hash')
                if run['sideEffect'] and not digest: raise ValueError('missing side effect identity')
            self.runs=loaded
        except FileNotFoundError:
            if self.marker.exists(): self.history_error='action_history_unavailable'
        except (ValueError,OSError,TypeError):
            self.history_error='action_history_unavailable'
        if self.history_error: return
        for run in self.runs.values():
            # Older results may already contain secrets, including now-rotated tokens.
            # Preserve identity/state and idempotency; never re-publish untrusted copies.
            if run.get('redactionVersion') != 5:
                run.pop('result',None)
                run['resultState']='redacted_legacy'
                run['redactionVersion']=5
            if run['state'] not in TERMINAL:
                run['state']='unknown' if run.get('sideEffect') else 'cancelled'
                run['error']={'code':'host_restarted'}
        if self.runs:
            try: self.save()
            except OSError: self.history_error='action_history_unavailable'

    def status(self):
        return {'state':'unavailable' if self.history_error else 'ready','error':self.history_error}

    def save(self):
        if self.history_error: raise KernelError(503,self.history_error)
        atomic(self.path,self.runs)
        if not self.marker.exists(): atomic(self.marker,{'version':1})

    def get(self,run_id):
        if self.history_error: raise KernelError(503,self.history_error)
        if run_id not in self.runs: raise KernelError(404,'run_not_found')
        return {k:v for k,v in self.runs[run_id].items() if k not in ('idempotencyHash','fingerprint')}

    async def start(self,plugin,action_id,uid,input,key=None):
        if self.history_error: raise KernelError(503,self.history_error)
        action=next((a for a in plugin.spec.actions if a['id']==action_id),None)
        if action is None: raise KernelError(404,'action_not_found')
        if action_id not in plugin.capabilities.get('actionIds',[]): raise KernelError(501,'unsupported')
        if not isinstance(input,dict): raise ValueError('action input must be object')
        validate(input,action.get('inputSchema',OBJECT))
        if action.get('scope','plugin')=='account' and not uid: raise ValueError('accountId required')
        if action.get('scope','plugin')=='plugin' and uid: raise ValueError('plugin action has no accountId')
        if uid is not None:
            from .credentials import account_id
            account_id(uid)
        if uid and (not plugin.account_pool or uid not in plugin.account_pool._accounts): raise KernelError(404,'account_not_found')
        side=action.get('idempotency','read')=='side_effect' or action.get('mutatesCredentials',False)
        if side and not key: raise ValueError('idempotencyKey required')
        if key is not None and (not isinstance(key,str) or not 1<=len(key)<=256): raise ValueError('invalid idempotencyKey')
        digest=hashlib.sha256(json.dumps([plugin.name,action_id,uid,key]).encode()).hexdigest() if key else None
        fingerprint=hashlib.sha256(json.dumps(input,sort_keys=True).encode()).hexdigest()
        if digest:
            for run in self.runs.values():
                if run.get('idempotencyHash')==digest:
                    if run['fingerprint']!=fingerprint: raise KernelError(409,'idempotency_conflict')
                    return self.get(run['id'])
        previous_runs=dict(self.runs)
        # Fail closed rather than forget idempotency keys for prior side effects.
        if len(self.runs)>=self.limit:
            removable=next((rid for rid,r in self.runs.items() if r['state'] in TERMINAL and not r.get('sideEffect')),None)
            if removable is None: raise KernelError(503,'action_history_full')
            self.runs.pop(removable)
        concurrency=(plugin.name,action.get('concurrencyKey',action_id),uid)
        if concurrency in self.keys: raise KernelError(409,'action_running')
        rid=uuid.uuid4().hex
        run={'id':rid,'plugin':plugin.name,'action':action_id,'accountId':uid,'state':'queued','createdAt':time.time(),
             'redactionVersion':5,'sideEffect':side,'idempotencyHash':digest,'fingerprint':fingerprint,'generation':plugin.generation}
        self.runs[rid]=run
        try:
            self.save()  # No worker can exist before its intent is persisted.
        except OSError:
            self.runs=previous_runs
            raise KernelError(503,'action_intent_write_failed') from None
        self.keys.add(concurrency)
        task=asyncio.create_task(self._execute(run,plugin,action,uid,input,concurrency))
        self.tasks[rid]=task
        task.add_done_callback(lambda _: self.tasks.pop(rid,None))
        return self.get(rid)

    async def _execute(self,run,plugin,action,uid,input,concurrency):
        dispatched=False
        try:
            run['state']='running'; self.save()
            async with self.kernel.maintenance(plugin,uid,allow_disabled=action.get('allowDisabled',False)) as account:
                generation=read(account.credential_file)['generation'] if account else None
                params={'action':action['id'],'input':input}
                if account: params['credential']={'id':uid,'secret':account.credential()}
                dispatched=True
                result=await plugin.transport.call('action.execute',params,min(action.get('timeoutSeconds',30),self.kernel.config.request_timeout_seconds))
                validate(result,action.get('outputSchema',OBJECT))
                if 'credentialUpdate' in result:
                    if not action.get('mutatesCredentials') or not account: raise ValueError('unexpected credential mutation')
                    await plugin.account_pool.rotate(uid,result['credentialUpdate'],expected_generation=generation,recovered=True,replace=True)
                    self.kernel.register_secrets(plugin)
                run['result']=self.kernel.public(self.kernel.public_action(protect(result,action.get('outputSchema',OBJECT))))
                run['state']='succeeded'
        except asyncio.CancelledError:
            run['state']='unknown' if run['sideEffect'] and dispatched else 'cancelled'
            run['error']={'code':'cancel_requested'}
        except Exception as exc:
            run['state']='unknown' if run['sideEffect'] and dispatched else 'failed'
            run['error']={'code':exc.code if isinstance(exc,KernelError) else 'timeout' if isinstance(exc,TimeoutError) else 'action_failed'}
        finally:
            run['finishedAt']=time.time()
            self.keys.discard(concurrency)
            try: self.save()
            except OSError: self.history_error='action_history_unavailable'

    async def wait(self,rid):
        task=self.tasks.get(rid)
        if task: await asyncio.shield(task)
        return self.get(rid)

    async def cancel(self,rid):
        run=self.get(rid)
        task=self.tasks.get(rid)
        if task:
            # Let the coroutine enter its try/finally before cancellation.
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        return self.get(rid)

    async def close(self):
        await asyncio.sleep(0)
        for task in list(self.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.tasks.values()),return_exceptions=True)
