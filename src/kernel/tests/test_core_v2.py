"""Host acceptance: two different v2 plugins, real Node subprocesses, local HTTP only."""
import asyncio
import copy
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nachuan.kernel import Kernel
from nachuan.app import create_app
from nachuan.contracts import validate_spec
from nachuan.schema import check_schema, validate
from nachuan.credentials import read, GenerationConflict
from nachuan.accounts import AccountPool
from nachuan.tasks import TaskSpec
from nachuan.ledger import Ledger, UsageRecord
from test_kernel import RUNTIME

HERE=Path(__file__).parent
FIXTURES=HERE.parents[1]/'contracts/schema-fixtures.json'


class SchemaTests(unittest.TestCase):
    def test_shared_fixtures(self):
        for f in json.loads(FIXTURES.read_text()):
            with self.subTest(f['name']):
                if f.get('schemaValid') is False:
                    with self.assertRaises(ValueError): check_schema(f['schema'])
                else:
                    check_schema(f['schema'])
                    if f['valid']: validate(f['value'],f['schema'])
                    else:
                        with self.assertRaises(ValueError): validate(f['value'],f['schema'])


class CoreV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.gate=asyncio.Event(); self.entered=asyncio.Event()
        self.hits=[]; self.quota_hits=0; self.quota_fail=False; self.quota_wait=False
        self.mode='normal'
        app=web.Application()
        async def validate_credential(request):
            value=await request.json()
            return web.json_response({'valid':'rejected' not in value.values()})
        async def chat(request):
            body=await request.json(); self.hits.append(('chat',body))
            return web.json_response({'id':'completion','choices':[], 'usage':{'total_tokens':3,'vendor':{'cost':'1.25','unit':'point','dimension':'chat'}}})
        async def action(request):
            body=await request.json(); self.hits.append(('action',body)); self.entered.set()
            if body['action']=='slow' or body.get('input',{}).get('x')==99: await self.gate.wait()
            if body['action']=='explode': raise web.HTTPServiceUnavailable()
            if body['action']=='mutate':
                return web.json_response({'credentialUpdate':{'cookie':'rotated-cookie-private','csrf':'rotated-csrf-private','workspace':'space-a'},'ok':True})
            return web.json_response({'ok':True,'echo':body.get('credential',{}).get('secret',{}),'nested':{'credentialUpdate':{'cookie':'must-never-appear'}},'privateReport':{'code':314159,'note':'private-action-value'}})
        async def quota(request):
            self.quota_hits+=1
            body=await request.json()
            if self.quota_wait: self.entered.set(); await self.gate.wait()
            if self.quota_fail: raise web.HTTPServiceUnavailable()
            now=time.time()
            if self.mode=='invalid': return web.json_response({'accountId':body['id'],'dimensions':[],'observedAt':now,'expiresAt':now-1})
            zero=self.mode=='zero'
            secret=body['secret']
            dimensions=[{'id':'tokens','kind':'tokens','unit':'token','remaining':'0' if zero else '100'}] if 'apiKey' in secret else [
                {'id':'requests','kind':'requests','unit':'request','remaining':'0' if zero else '85','limit':'100','resetAt':now+300},
                {'id':'points','kind':'points','unit':'point','remaining':'10'},
                {'id':'seat','kind':'seat','status':'active'},
                {'id':'batches','kind':'batches','unit':'point','batches':[{'batchId':'expired','remaining':'900','expiresAt':now-1},{'batchId':'valid','remaining':'5','expiresAt':now+300}]},
            ]
            return web.json_response({'accountId':body['id'],'observedAt':now,'expiresAt':now+30,'dimensions':dimensions})
        for route,handler in [('/chat',chat),('/action',action),('/quota',quota),('/validate',validate_credential)]: app.router.add_post(route,handler)
        self.upstream=TestServer(app); await self.upstream.start_server(); self.addAsyncCleanup(self.upstream.close)
        self.specs={}
        for name,fields in [('token',['apiKey']),('session',['cookie','csrf','workspace'])]:
            p=self.root/'plugins'/f'2api-plugin-{name}'; p.mkdir(parents=True)
            shutil.copyfile(HERE/'fixtures/plugin-v2/index.ts',p/'index.ts')
            spec={'name':p.name,'version':'2.0','specVersion':2,'runtime':{'entry':'index.ts'},
                  'models':[{'id':'mock','capabilities':['chat','reasoning'],'optionsSchema':{'type':'object','properties':{'thinking_budget':{'type':'integer','minimum':0}},'additionalProperties':False}}],
                  'credentialSchema':{'type':'object','properties':{f:{'type':'string','minLength':1} for f in fields},'required':fields,'additionalProperties':False},
                  'config':{'baseUrl':str(self.upstream.make_url('')).rstrip('/')},
                  'quota':{'query':True,'ttlSeconds':60,'reliableExhaustion':True},
                  'actions':[{'id':a,'scope':'account','timeoutSeconds':1,'idempotency':'side_effect' if a in ('grant','mutate') else 'read','mutatesCredentials':a=='mutate',
                              'inputSchema':{'type':'object','properties':{'x':{'type':'integer'}},'additionalProperties':False},'outputSchema':{'type':'object','properties':{'privateReport':{'type':'object','secret':True}}}} for a in ('probe','slow','grant','mutate','explode','unsupported')],
                  'tasks':[{'action':'probe','intervalSeconds':3600,'accountId':'one'}]}
            (p/'spec.json').write_text(json.dumps(spec)); self.specs[name]=spec
        self.kernel=Kernel(self.root/'plugins',runtime_dir=RUNTIME)
        await self.kernel.load()
        self.client=TestClient(TestServer(create_app(self.kernel,'gateway-key',self.root/'config.json')))
        await self.client.start_server(); self.addAsyncCleanup(self.client.close)
        self.headers={'Authorization':'Bearer gateway-key'}

    def url(self,name,suffix=''): return '/kernel/plugins/2api-plugin-'+name+suffix
    async def request(self,method,url,body=None):
        response=await self.client.request(method,url,headers=self.headers,json=body)
        return response,await response.json()
    async def add(self,name='session'):
        secret={'apiKey':'api-key-private'} if name=='token' else {'cookie':'cookie-private-value','csrf':'csrf-private-value','workspace':'space-a'}
        response,body=await self.request('POST',self.url(name,'/accounts'),{'id':'one','credential':secret,'maxInFlight':1})
        self.assertEqual(response.status,201,body)
        return secret
    async def run_action(self,action='probe',name='session',**kwargs):
        response,run=await self.request('POST',self.url(name,f'/actions/{action}/runs'),{'accountId':'one','input':{},**kwargs})
        self.assertEqual(response.status,202,run)
        return await self.kernel.actions.wait(run['id'])

    async def test_discovery_without_accounts_and_two_plugins_chat(self):
        for name in ('token','session'):
            response,descriptor=await self.request('GET',self.url(name,'/descriptor'))
            self.assertEqual(response.status,200)
            self.assertEqual(descriptor['protocol'],2)
            self.assertFalse(next(a for a in descriptor['actions'] if a['id']=='unsupported')['supported'])
            await self.add(name)
        response,models=await self.request('GET','/v1/models')
        self.assertEqual(len(models['data']),2)
        for name in ('token','session'):
            response,body=await self.request('POST','/v1/chat/completions',{'model':name+':mock','messages':[]})
            self.assertEqual(response.status,200,body)
        secret=self.hits[-1][1]['credential']['secret']
        self.assertEqual(secret['cookie'],'cookie-private-value')
        response,models=await self.request('GET','/kernel/models')
        self.assertIn('reasoning',models['models'][0]['capabilities'])

    async def test_schema_validation_and_unsupported_actions(self):
        response,_=await self.request('POST',self.url('session','/accounts'),{'id':'one','credential':{'apiKey':'wrong'}})
        self.assertEqual(response.status,400)
        self.assertFalse((self.root/'plugins/2api-plugin-session/credentials/one.json').exists())
        response,_=await self.request('POST',self.url('session','/accounts'),{'id':'one','credential':{'cookie':'rejected','csrf':'valid','workspace':'valid'}})
        self.assertEqual(response.status,400)
        await self.add()
        response,_=await self.request('POST',self.url('session','/actions/unsupported/runs'),{'accountId':'one','input':{}})
        self.assertEqual(response.status,501)
        response,_=await self.request('POST',self.url('session','/actions/probe/runs'),{'accountId':'one','input':{'x':'bad'}})
        self.assertEqual(response.status,400)
        response,_=await self.request('POST','/v1/chat/completions',{'model':'session:mock','messages':[],'thinking_budget':-1})
        self.assertEqual(response.status,400)

    async def test_rotate_generation_disable_and_restart(self):
        await self.add()
        new={'cookie':'new-private-cookie','csrf':'new-private-csrf','workspace':'space-b'}
        async def rotate(): return await self.request('POST',self.url('session','/accounts/one/rotate'),{'credential':new,'expectedGeneration':1})
        results=await asyncio.gather(rotate(),rotate())
        self.assertEqual(sorted(r.status for r,b in results),[200,409])
        response,_=await self.request('PATCH',self.url('session','/accounts/one'),{'enabled':False,'maxInFlight':2})
        self.assertEqual(response.status,200)
        await self.kernel.load()
        pool=self.kernel.plugin('2api-plugin-session').account_pool
        self.assertFalse(pool._accounts['one'].enabled)
        path=pool._accounts['one'].credential_file
        self.assertEqual(read(path)['secret'],new)
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        # A separate kernel process lifetime reads the same persisted disabled state.
        await self.kernel.close()
        other=Kernel(self.root/'plugins',runtime_dir=RUNTIME); self.addAsyncCleanup(other.close)
        await other.load()
        self.assertFalse(other.plugin('2api-plugin-session').account_pool._accounts['one'].enabled)

    async def test_actions_idempotency_secrets_and_credential_commit(self):
        secret=await self.add()
        first=await self.run_action('grant',idempotencyKey='same')
        second=await self.run_action('grant',idempotencyKey='same')
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(sum(b.get('action')=='grant' for kind,b in self.hits),1)
        self.assertEqual(first['state'],'succeeded')
        text=json.dumps(first)
        for value in secret.values(): self.assertNotIn(value,text)
        self.assertNotIn('credentialUpdate',text)
        self.assertNotIn('private-action-value',text)
        self.assertEqual(first['result']['privateReport'],'••••')
        updated=await self.run_action('mutate',idempotencyKey='rotate-once')
        self.assertEqual(updated['state'],'succeeded')
        account=self.kernel.plugin('2api-plugin-session').account_pool._accounts['one']
        self.assertEqual(account.credential()['cookie'],'rotated-cookie-private')
        self.assertNotIn('rotated-cookie-private',json.dumps(updated))
        await self.kernel.load()
        repeated=await self.run_action('grant',idempotencyKey='same')
        self.assertEqual(repeated['id'],first['id'])
        for endpoint in (self.url('session','/accounts'),'/kernel/monitor','/kernel/plugins'):
            _,body=await self.request('GET',endpoint)
            for value in (*secret.values(),'rotated-cookie-private'): self.assertNotIn(value,json.dumps(body))

    async def test_action_cancel_timeout_and_reload_release_permits(self):
        await self.add()
        response,run=await self.request('POST',self.url('session','/actions/slow/runs'),{'accountId':'one','input':{}})
        await asyncio.wait_for(self.entered.wait(),2)
        await self.kernel.load()
        response,cancelled=await self.request('POST','/kernel/action-runs/'+run['id']+'/cancel')
        self.assertEqual(cancelled['state'],'cancelled')
        self.assertEqual(self.kernel._plugin_active.get('2api-plugin-session'),0)
        self.entered.clear()
        result=await self.run_action('slow')
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['error']['code'],'timeout')
        self.assertEqual(self.kernel._account_active.get(('2api-plugin-session','one')),0)
        self.gate.set()
        self.assertEqual((await self.run_action('probe'))['state'],'succeeded')

    async def test_scheduler_uses_same_action_runner(self):
        await self.add()
        spec=TaskSpec('2api-plugin-session:probe','2api-plugin-session','probe',3600)
        self.assertTrue(await self.kernel.scheduler.run(spec,self.kernel.run_task))
        self.assertEqual(len(self.kernel.actions.runs),1)
        self.assertIsNotNone(self.kernel.scheduler.snapshot()['tasks'][0]['nextRunAt'])

    async def test_quota_types_cache_exhaustion_and_generation_fence(self):
        await self.add('token'); await self.add('session')
        for name in ('token','session'):
            response,data=await self.request('POST',self.url(name,'/accounts/one/quota/refresh'))
            self.assertEqual(response.status,200,data)
            self.assertEqual(data['state'],'known')
        session=self.kernel.plugin('2api-plugin-session')
        data=self.kernel.quota.get(session,'one')
        dims={d['kind']:d for d in data['snapshot']['dimensions']}
        self.assertEqual(dims['batches']['remaining'],'5')
        self.assertEqual(dims['seat']['status'],'active')
        self.mode='zero'
        await self.request('POST',self.url('token','/accounts/one/quota/refresh'))
        response,_=await self.request('POST','/v1/chat/completions',{'model':'token:mock','messages':[]})
        self.assertEqual(response.status,503)
        self.mode='normal'; self.quota_fail=True
        response,_=await self.request('POST',self.url('session','/accounts/one/quota/refresh'))
        self.assertEqual(response.status,502)
        self.assertEqual(self.kernel.quota.get(session,'one')['state'],'error')
        self.assertEqual(self.kernel.quota.get(session,'one')['snapshot']['dimensions'][0]['remaining'],'85')
        self.quota_fail=False; self.quota_wait=True; self.entered.clear()
        task=asyncio.create_task(self.kernel.quota.refresh(session,'one'))
        await asyncio.wait_for(self.entered.wait(),2)
        await session.account_pool.rotate('one',{'cookie':'other-cookie','csrf':'other-csrf','workspace':'other-space'},expected_generation=1,replace=True)
        self.gate.set()
        with self.assertRaises(Exception) as failure: await task
        self.assertEqual(failure.exception.status,409)
        self.assertEqual(self.kernel.quota.get(session,'one')['state'],'unknown')

    async def test_quota_coalesces_and_stale_does_not_block(self):
        await self.add('token')
        plugin=self.kernel.plugin('2api-plugin-token')
        before=self.quota_hits
        await asyncio.gather(*(self.kernel.quota.refresh(plugin,'one') for _ in range(8)))
        self.assertEqual(self.quota_hits-before,1)
        self.mode='zero'
        await self.kernel.quota.refresh(plugin,'one')
        self.assertTrue(self.kernel.quota.exhausted(plugin,'one'))
        self.kernel.quota.records[plugin.name+':one']['snapshot']['expiresAt']=time.time()-1
        self.assertEqual(self.kernel.quota.get(plugin,'one')['state'],'stale')
        self.assertFalse(self.kernel.quota.exhausted(plugin,'one'))

    async def test_mixed_unit_settlements_and_stream_vendor_usage(self):
        await self.add('token')
        response=await self.client.post('/v1/chat/completions',headers=self.headers,json={'model':'token:mock','messages':[],'stream':True})
        self.assertEqual(response.status,200)
        await response.read(); await asyncio.sleep(0)
        await self.kernel.ledger._queue.join()
        data=self.kernel.ledger.snapshot()
        self.assertEqual(data['settlements'][0]['cost'],'1.25')
        self.assertEqual(data['settlements'][0]['dimension'],'chat')
        ledger=Ledger(self.root/'mixed.json')
        for i,(plugin,unit,cost) in enumerate([('a','point',100),('a','CNY',2),('b','point',50)]):
            await ledger.record(UsageRecord(str(i),time.time(),plugin,'m','a',False,True,1,1,1,2,'upstream',cost,unit))
        data=ledger.snapshot()
        self.assertEqual(len(data['settlements']),3)
        self.assertIsNone(data['totals']['vendor_cost'])
        self.assertEqual(data['settlement_state'],'mixed')

    async def test_invalid_reload_preserves_last_good_and_disk_config(self):
        await self.add()
        plugin=self.kernel.plugin('2api-plugin-session')
        old=plugin.transport.process.pid
        path=plugin.directory/'spec.json'
        bad=copy.deepcopy(self.specs['session']); bad['actions'][0]['required']=True; bad['actions'][0]['id']='unsupported'
        # Duplicate action IDs are rejected before starting a replacement.
        path.write_text(json.dumps(bad))
        response,_=await self.request('PUT','/kernel/config',{'requestTimeoutSeconds':30})
        self.assertEqual(response.status,400)
        self.assertEqual(self.kernel.plugin('2api-plugin-session').transport.process.pid,old)
        self.assertFalse((self.root/'config.json').exists())
        self.assertEqual((await self.run_action('probe'))['state'],'succeeded')

    async def test_crash_isolates_plugin_and_side_effect_is_not_replayed(self):
        await self.add('token'); await self.add('session')
        response,run=await self.request('POST',self.url('session','/actions/grant/runs'),{'accountId':'one','input':{'x':99},'idempotencyKey':'side-effect-once'})
        self.assertEqual(response.status,202)
        await asyncio.wait_for(self.entered.wait(),2)
        plugin=self.kernel.plugin('2api-plugin-session')
        plugin.transport.process.terminate()
        await plugin.transport.process.wait()
        terminal=await self.kernel.actions.wait(run['id'])
        self.assertEqual(terminal['state'],'unknown')
        response,_=await self.request('POST','/v1/chat/completions',{'model':'token:mock','messages':[]})
        self.assertEqual(response.status,200)
        self.assertEqual(self.kernel._account_active[('2api-plugin-session','one')],0)
        await self.kernel.close()
        other=Kernel(self.root/'plugins',runtime_dir=RUNTIME); self.addAsyncCleanup(other.close)
        await other.load()
        repeated=await other.actions.start(other.plugin('2api-plugin-session'),'grant','one',{'x':99},'side-effect-once')
        self.assertEqual(repeated['id'],run['id'])
        self.assertEqual(repeated['state'],'unknown')
        self.assertEqual(sum(kind=='action' and body['action']=='grant' for kind,body in self.hits),1)
        self.gate.set()

    async def test_migration_and_parallel_generation_across_pools(self):
        directory=self.root/'migration'; directory.mkdir()
        path=directory/'a.json'
        original={'apiKey':'old-private-key','enabled':True,'maxInFlight':1}
        path.write_text(json.dumps(original))
        pool=AccountPool(directory); pool.load()
        other=AccountPool(directory); other.load()
        self.assertEqual(json.loads(path.read_text()),original)
        async def rotate(pool,value):
            try:
                await pool.rotate('a',{'apiKey':value},expected_generation=1,replace=True)
                return 'ok'
            except GenerationConflict: return 'conflict'
        results=await asyncio.gather(rotate(pool,'new-private-1'),rotate(other,'new-private-2'))
        self.assertEqual(sorted(results),['conflict','ok'])
        self.assertEqual(read(path)['generation'],2)
        backup=path.with_suffix('.migration.bak')
        self.assertEqual(json.loads(backup.read_text()),original)
        self.assertEqual(backup.stat().st_mode&0o777,0o600)
        await pool.close(); await other.close()

    async def test_missing_handler_required_rejects_reload_and_retains_runtime(self):
        await self.add()
        plugin=self.kernel.plugin('2api-plugin-session')
        old_pid=plugin.transport.process.pid
        bad=copy.deepcopy(self.specs['session']); bad['actions'][-1]['required']=True
        (plugin.directory/'spec.json').write_text(json.dumps(bad))
        response,_=await self.request('PUT','/kernel/config',{'requestTimeoutSeconds':30})
        self.assertEqual(response.status,400)
        self.assertEqual(self.kernel.plugin('2api-plugin-session').transport.process.pid,old_pid)
        self.assertEqual((await self.run_action('probe'))['state'],'succeeded')

    async def test_load_shed_and_cancel_stress_keeps_host_counters_balanced(self):
        await self.add('token'); await self.add('session')
        for _ in range(3):
            self.entered.clear(); self.gate.clear()
            response,run=await self.request('POST',self.url('session','/actions/slow/runs'),{'accountId':'one','input':{}})
            await asyncio.wait_for(self.entered.wait(),2)
            requests=[self.request('POST','/v1/chat/completions',{'model':'session:mock','messages':[]}) for _ in range(12)]
            responses=await asyncio.gather(*requests)
            self.assertTrue(all(r.status in (429,503) for r,b in responses))
            await self.kernel.load()
            await self.kernel.actions.cancel(run['id'])
            self.gate.set()
            response,_=await self.request('POST','/v1/chat/completions',{'model':'token:mock','messages':[]})
            self.assertEqual(response.status,200)
        self.assertTrue(all(value==0 for value in self.kernel._plugin_active.values()))
        self.assertTrue(all(value==0 for value in self.kernel._account_active.values()))
        await asyncio.sleep(0); await self.kernel.ledger._queue.join()
        records=self.kernel.ledger.snapshot(limit=500)['records']
        self.assertEqual(sum(r['success'] for r in records),3)

    async def test_reload_shares_account_state_and_disabled_can_be_reenabled(self):
        from nachuan.accounts import Failure, FailureKind, AccountState
        await self.add('session')
        old=self.kernel.plugin('2api-plugin-session').account_pool
        lease=await old.acquire()
        await self.kernel.load()
        new=self.kernel.plugin('2api-plugin-session').account_pool
        self.assertIs(old._accounts['one'],new._accounts['one'])
        await lease.release(Failure(FailureKind.REAUTH,message='expired'))
        self.assertEqual(new._accounts['one'].state,AccountState.REAUTH_REQUIRED)
        with self.assertRaises(RuntimeError): await new.acquire()
        await new.patch('one',enabled=False)
        await self.kernel.load()
        latest=self.kernel.plugin('2api-plugin-session').account_pool
        await latest.patch('one',enabled=True)
        await self.kernel.load()
        current=self.kernel.plugin('2api-plugin-session').account_pool
        self.assertTrue(current._accounts['one'].enabled)
        self.assertEqual(current._accounts['one'].state,AccountState.READY)
        await old.save()
        await current.save()
        self.assertEqual(read(current._accounts['one'].credential_file)['account']['enabled'],True)

    async def test_exhaustion_without_reset_requires_recovery(self):
        from nachuan.accounts import Failure, FailureKind, AccountState
        await self.add('token')
        plugin=self.kernel.plugin('2api-plugin-token')
        lease=await plugin.account_pool.acquire()
        await lease.release(Failure(FailureKind.EXHAUSTED))
        self.assertEqual(lease.account.until,0)
        with self.assertRaises(RuntimeError): await plugin.account_pool.acquire()
        await self.kernel.quota.refresh(plugin,'one')
        self.assertEqual(lease.account.state,AccountState.READY)

    async def test_two_account_scheduled_actions_have_separate_task_ids(self):
        await self.add('session')
        response,_=await self.request('POST',self.url('session','/accounts'),{'id':'two','credential':{'cookie':'cookie-two-private','csrf':'csrf-two-private','workspace':'space-two'}})
        self.assertEqual(response.status,201)
        spec=copy.deepcopy(self.specs['session'])
        spec['tasks']=[{'id':uid+'_probe','action':'probe','accountId':uid,'intervalSeconds':3600} for uid in ('one','two')]
        (self.kernel.plugin('2api-plugin-session').directory/'spec.json').write_text(json.dumps(spec))
        await self.kernel.load()
        for uid in ('one','two'):
            response,body=await self.request('POST',f'/kernel/tasks/2api-plugin-session:{uid}_probe/run')
            self.assertTrue(body['ok'],body)
        self.assertEqual(sorted(r['accountId'] for r in self.kernel.actions.runs.values()),['one','two'])

    async def test_unregister_cancels_manual_task_action_with_terminal_record(self):
        await self.add()
        plugin=self.kernel.plugin('2api-plugin-session')
        spec=copy.deepcopy(self.specs['session'])
        spec['tasks']=[{'action':'slow','intervalSeconds':3600,'accountId':'one'}]
        (plugin.directory/'spec.json').write_text(json.dumps(spec)); await self.kernel.load()
        task_spec=TaskSpec('2api-plugin-session:slow','2api-plugin-session','slow',3600)
        manual=asyncio.create_task(self.kernel.scheduler.run(task_spec,self.kernel.run_task))
        await asyncio.wait_for(self.entered.wait(),2)
        await self.kernel.scheduler.unregister(task_spec.id)
        with self.assertRaises(asyncio.CancelledError): await manual
        self.assertEqual(next(iter(self.kernel.actions.runs.values()))['state'],'cancelled')
        self.assertEqual(self.kernel._account_active[('2api-plugin-session','one')],0)
        self.assertFalse(self.kernel.scheduler._running)
        self.gate.set()
