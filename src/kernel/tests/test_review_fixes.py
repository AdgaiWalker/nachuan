"""Regression gates for the six V2 review findings; local fake upstream only."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_core_v2 as base
from nachuan.actions import ActionRunner
from nachuan.accounts import AccountPool, AccountState, Failure, FailureKind
from nachuan.credentials import read
from nachuan.kernel import Kernel
from test_kernel import write_plugin, RUNTIME
from aiohttp import web
from aiohttp.test_utils import TestServer


class ReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=base.CoreV2Tests()
        await self.h.asyncSetUp()
        self.addAsyncCleanup(self.cleanup_harness)

    async def cleanup_harness(self):
        self.h.gate.set()
        await self.h.client.close()
        await self.h.upstream.close()
        self.h.tmp.cleanup()

    async def test_corrupt_history_blocks_replay_and_preserves_evidence(self):
        h=self.h
        await h.add()
        first=await h.run_action('grant',idempotencyKey='do-once')
        path=h.kernel.actions.path
        for bad in ('{broken','[]',json.dumps({'bad':{'id':'bad'}})):
            with self.subTest(bad=bad):
                path.write_text(bad)
                h.kernel.actions=ActionRunner(h.kernel,path)
                response,body=await h.request('POST',h.url('session','/actions/grant/runs'),{'accountId':'one','input':{},'idempotencyKey':'do-once'})
                self.assertEqual(response.status,503,body)
                self.assertEqual(body['error']['code'],'action_history_unavailable')
                self.assertEqual(path.read_text(),bad)
        self.assertEqual(sum(kind=='action' and body['action']=='grant' for kind,body in h.hits),1)
        response,status=await h.request('GET','/kernel/status')
        self.assertEqual(status['actionHistory']['state'],'unavailable')

    async def test_unreadable_history_is_not_treated_as_first_boot(self):
        h=self.h
        await h.add()
        await h.run_action('grant',idempotencyKey='unreadable-once')
        path=h.kernel.actions.path; before=path.read_bytes()
        original=Path.read_text
        def read_text(p,*args,**kwargs):
            if p==path: raise PermissionError('simulated read failure')
            return original(p,*args,**kwargs)
        with patch.object(Path,'read_text',read_text):
            h.kernel.actions=ActionRunner(h.kernel,path)
        response,_=await h.request('POST',h.url('session','/actions/grant/runs'),{'accountId':'one','input':{},'idempotencyKey':'unreadable-once'})
        self.assertEqual(response.status,503)
        self.assertEqual(path.read_bytes(),before)

    async def test_intent_write_failure_rolls_back_and_retry_executes(self):
        h=self.h
        await h.add()
        runner=h.kernel.actions
        prior=await h.run_action('probe')
        runner.limit=1  # Failed writes must also restore an evicted read-only run.
        before=copy.deepcopy(runner.runs)
        with patch.object(runner,'save',side_effect=OSError('disk full')):
            response,_=await h.request('POST',h.url('session','/actions/grant/runs'),{'accountId':'one','input':{},'idempotencyKey':'disk-once'})
        self.assertEqual(response.status,503)
        self.assertEqual(runner.runs,before)
        run=await h.run_action('grant',idempotencyKey='disk-once')
        self.assertEqual(run['state'],'succeeded')
        self.assertEqual(sum(kind=='action' and body['action']=='grant' for kind,body in h.hits),1)

    async def test_schema_suffix_results_are_redacted_before_storage_and_http(self):
        h=self.h
        await h.add('token')
        plugin=h.kernel.plugin('2api-plugin-token')
        original=plugin.transport.call
        async def call(method,params,timeout):
            if method=='action.execute':
                token=params['credential']['secret']['apiKey']
                return {'debugSchema':{'copiedToken':token,'secret':token,token:'visible'},'nested':[{'otherSchema':{'token':token,token:'visible'}}],'ordinary':'visible'}
            return await original(method,params,timeout)
        with patch.object(plugin.transport,'call',side_effect=call):
            run=await h.run_action('probe',name='token')
        self.assertEqual(run['state'],'succeeded')
        self.assertEqual(run['result']['ordinary'],'visible')
        self.assertNotIn('api-key-private',json.dumps(run))
        self.assertNotIn('api-key-private',h.kernel.actions.path.read_text())
        for method,suffix in [('GET',''),('POST','/cancel')]:
            response,body=await h.request(method,'/kernel/action-runs/'+run['id']+suffix)
            self.assertEqual(response.status,200)
            self.assertNotIn('api-key-private',json.dumps(body))
        # Challenge a sibling path: legacy scheduled result serialization.
        self.assertNotIn('api-key-private',json.dumps(h.kernel.public({'nested':[{'fooSchema':{'credentialUpdate':'api-key-private','value':'api-key-private'}}]})))

    async def test_descriptors_keep_reserved_schema_properties_and_annotations(self):
        h=self.h
        spec=copy.deepcopy(h.specs['token'])
        metadata={'type':'object','properties':{'secret':{'type':'string','secret':True},'credential':{'type':'string'},'credentialUpdate':{'type':'string'}}}
        spec['credentialSchema']['properties'].update(metadata['properties'])
        spec['models'][0]['optionsSchema']=metadata
        spec['actions'][0]['inputSchema']=metadata
        spec['actions'][0]['outputSchema']=metadata
        p=h.kernel.plugin('2api-plugin-token')
        (p.directory/'spec.json').write_text(json.dumps(spec))
        await h.kernel.load()
        for endpoint in (h.url('token','/descriptor'),'/kernel/models',h.url('token','/actions')):
            response,body=await h.request('GET',endpoint)
            self.assertEqual(response.status,200)
            data=json.dumps(body)
            self.assertIn('"secret": true',data)
            self.assertIn('"credentialUpdate"',data)
            self.assertIn('"credential"',data)

    async def test_zero_or_empty_quota_does_not_recover_exhausted_account(self):
        h=self.h
        await h.add('token')
        p=h.kernel.plugin('2api-plugin-token')
        spec=copy.deepcopy(h.specs['token']); spec['quota']['reliableExhaustion']=False
        (p.directory/'spec.json').write_text(json.dumps(spec)); await h.kernel.load()
        p=h.kernel.plugin('2api-plugin-token')
        lease=await p.account_pool.acquire(); await lease.release(Failure(FailureKind.EXHAUSTED))
        h.mode='zero'; await h.kernel.quota.refresh(p,'one')
        self.assertEqual(lease.account.state,AccountState.EXHAUSTED)
        original=p.transport.call
        async def empty(method,params,timeout):
            if method=='quota.query': return {'accountId':'one','observedAt':time.time(),'expiresAt':time.time()+30,'dimensions':[]}
            return await original(method,params,timeout)
        with patch.object(p.transport,'call',side_effect=empty): await h.kernel.quota.refresh(p,'one')
        self.assertEqual(lease.account.state,AccountState.EXHAUSTED)
        h.mode='normal'; await h.kernel.quota.refresh(p,'one')
        self.assertEqual(lease.account.state,AccountState.READY)

    async def test_legacy_access_token_survives_load_migration_and_chat(self):
        observed=[]
        async def upstream(request):
            observed.append(request.headers.get('Authorization'))
            return web.json_response({'choices':[]})
        app=web.Application(); app.router.add_post('/chat',upstream)
        server=TestServer(app); await server.start_server(); self.addAsyncCleanup(server.close)
        directory=self.h.root/'legacy'
        p=write_plugin(directory,'2api-plugin-legacy',base_url=str(server.make_url('')).rstrip('/'))
        credential=p/'credentials/default.json'
        credential.write_text(json.dumps({'accessToken':'legacy-token-private'}))
        kernel=Kernel(directory,runtime_dir=RUNTIME); self.addAsyncCleanup(kernel.close)
        await kernel.load()
        route=kernel.resolve('legacy:mock')
        self.assertIsNotNone(route)
        plugin,model=route
        await kernel.invoke(plugin,{'messages':[]},model)
        self.assertEqual(observed,['Bearer legacy-token-private'])
        await plugin.account_pool.rotate('default',{'accessToken':'rotated-legacy-private'},replace=True)
        self.assertEqual(read(credential)['secret'],{'accessToken':'rotated-legacy-private'})
        # Empty or unrelated credentials must not become valid via compatibility.
        for value in ({},{'apiKey':''},{'accessToken':''},{'cookie':'not-a-token'}):
            with self.assertRaises(ValueError): await plugin.account_pool.add('invalid',value)

    async def test_api_account_refresh_uses_negotiated_handler_and_policy(self):
        h=self.h
        p=h.kernel.plugin('2api-plugin-token')
        spec=copy.deepcopy(h.specs['token']); spec['credentialSchema']['properties']['expiresAt']={'type':'number'}
        (p.directory/'spec.json').write_text(json.dumps(spec))
        entry=p.directory/'index.ts'
        entry.write_text(entry.read_text().replace('healthCheck:async()=>',"refreshCredential:async credential=>{ await call('/action',{action:'auto_refresh'}); return {...credential.secret,expiresAt:Date.now()/1000+3600} },\n    healthCheck:async()=>"))
        await h.kernel.load()
        response,body=await h.request('POST',h.url('token','/accounts'),{'id':'one','credential':{'apiKey':'refresh-private-key','expiresAt':time.time()+30}})
        self.assertEqual(response.status,201,body)
        p=h.kernel.plugin('2api-plugin-token')
        self.assertTrue(p.capabilities.get('credentialRefresh'))
        await h.kernel.refresh_credentials(p)
        self.assertEqual(sum(kind=='action' and body.get('action')=='auto_refresh' for kind,body in h.hits),1)
        self.assertGreater(p.account_pool._accounts['one'].credential()['expiresAt'],time.time()+3000)
        response,_=await h.request('PATCH',h.url('token','/accounts/one'),{'refreshEnabled':False})
        self.assertEqual(response.status,200)
        await p.account_pool.rotate('one',{'expiresAt':time.time()+30})
        await h.kernel.load()
        await h.kernel.refresh_credentials(h.kernel.plugin('2api-plugin-token'))
        self.assertEqual(sum(kind=='action' and body.get('action')=='auto_refresh' for kind,body in h.hits),1)
        response,_=await h.request('PATCH',h.url('token','/accounts/one'),{'refreshEnabled':True})
        self.assertEqual(response.status,200)
        self.assertTrue(h.kernel.plugin('2api-plugin-token').account_pool._accounts['one'].refresh_supported)
        await h.kernel.refresh_credentials(h.kernel.plugin('2api-plugin-token'))
        self.assertEqual(sum(kind=='action' and body.get('action')=='auto_refresh' for kind,body in h.hits),2)

    async def test_missing_initialized_history_also_blocks_replay(self):
        h=self.h
        await h.add()
        await h.run_action('grant',idempotencyKey='missing-once')
        path=h.kernel.actions.path
        path.unlink()
        h.kernel.actions=ActionRunner(h.kernel,path)
        response,_=await h.request('POST',h.url('session','/actions/grant/runs'),{'accountId':'one','input':{},'idempotencyKey':'missing-once'})
        self.assertEqual(response.status,503)
        self.assertFalse(path.exists())

    async def test_old_unsafe_results_are_scrubbed_without_losing_idempotency(self):
        h=self.h
        await h.add()
        first=await h.run_action('grant',idempotencyKey='old-result-once')
        path=h.kernel.actions.path
        saved=json.loads(path.read_text())
        saved[first['id']]['redactionVersion']=2
        saved[first['id']]['result']={'debugSchema':{'copiedToken':'old-rotated-token-not-in-registry'}}
        path.write_text(json.dumps(saved))
        h.kernel.actions=ActionRunner(h.kernel,path)
        response,body=await h.request('GET','/kernel/action-runs/'+first['id'])
        self.assertEqual(response.status,200)
        self.assertNotIn('old-rotated-token-not-in-registry',json.dumps(body))
        self.assertNotIn('old-rotated-token-not-in-registry',path.read_text())
        self.assertEqual(body['redactionVersion'],5)
        self.assertEqual(body['resultState'],'redacted_legacy')
        repeated=await h.run_action('grant',idempotencyKey='old-result-once')
        self.assertEqual(first['id'],repeated['id'])
        self.assertEqual(sum(kind=='action' and body['action']=='grant' for kind,body in h.hits),1)
