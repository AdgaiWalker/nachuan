import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_core_v2 as base
from nachuan.accounts import AccountPool, FailureKind
from nachuan.contracts import validate_spec
from nachuan.errors import PluginFailure
from nachuan.schema import validate
from nachuan.transport import ProcessTransport


class ReviewV22(unittest.IsolatedAsyncioTestCase):
    async def test_fast_stream_preserves_all_chunks(self):
        script="const r=require('readline').createInterface({input:process.stdin});r.on('line',l=>{let x=JSON.parse(l);if(!x.id)return;for(let i=0;i<1000;i++)process.stdout.write(JSON.stringify({id:x.id,method:'chunk',params:String(i)})+'\\n');process.stdout.write(JSON.stringify({id:x.id,method:'end'})+'\\n')})"
        with tempfile.TemporaryDirectory() as d:
            t=ProcessTransport(['node','-e',script],d)
            try:
                items=[x async for x in t.stream('stream',{},5)]
                self.assertEqual(items,[str(i) for i in range(1000)])
            finally: await t.close()

    async def test_incomplete_envelope_does_not_block_valid_account(self):
        with tempfile.TemporaryDirectory() as d:
            p=AccountPool(d); await p.add('a',{'apiKey':'x'}); await p.add('b',{'apiKey':'y'})
            path=p._accounts['a'].credential_file; value=json.loads(path.read_text()); del value['account']['maxInFlight']; path.write_text(json.dumps(value))
            lease=await p.acquire(); self.assertEqual(lease.account.id,'b'); await lease.release(None)
            p.load(); self.assertEqual(list(p._accounts),['b']); await p.close()

    async def test_old_account_reason_is_removed_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            p=AccountPool(d); await p.add('a',{'apiKey':'x'}); await p.close()
            path=Path(d)/'state.json'
            path.write_text(json.dumps({'accounts':{'a':{'state':'reauth_required','reason':'old-unknown-secret','failures':2}}}))
            p=AccountPool(d); p.load()
            self.assertEqual(p._accounts['a'].state,'reauth_required')
            self.assertEqual(p._accounts['a'].failures,2)
            self.assertNotIn('old-unknown-secret',path.read_text())
            await p.close()

    def test_invalid_schema_and_nested_enum_types(self):
        with self.assertRaises(ValueError): validate_spec({'name':'2api-plugin-x','version':'1','models':[{'id':'m'}],'credentialSchema':{'type':[]}},'.')
        schema={'type':'object','enum':[{'level':1,'nested':[False]}]}
        validate({'level':1.0,'nested':[False]},schema)
        with self.assertRaises(ValueError): validate({'level':True,'nested':[False]},schema)
        with self.assertRaises(ValueError): validate({'level':1,'nested':[0]},schema)


class ReviewV22Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=base.CoreV2Tests(); await self.h.asyncSetUp()

    async def asyncTearDown(self):
        self.h.gate.set(); await self.h.client.close(); await self.h.upstream.close(); self.h.tmp.cleanup()

    async def test_numeric_boolean_secret_action_and_history_migration(self):
        h=self.h; p=h.kernel.plugin('2api-plugin-token'); spec=copy.deepcopy(h.specs['token'])
        spec['credentialSchema']['properties'].update({'pin':{'type':'integer'},'flag':{'type':'boolean'}})
        (p.directory/'spec.json').write_text(json.dumps(spec)); await h.kernel.load()
        response,_=await h.request('POST',h.url('token','/accounts'),{'id':'one','credential':{'apiKey':'private-token','pin':314159,'flag':True}})
        self.assertEqual(response.status,201)
        run=await h.run_action('grant',name='token',idempotencyKey='only-once')
        self.assertEqual(run['result']['echo']['pin'],'••••'); self.assertEqual(run['result']['echo']['flag'],'••••')
        self.assertNotIn('3.14159e5',h.kernel.public_action('pin=3.14159e5 flag=true'))
        self.assertNotIn('true',h.kernel.public_action('pin=3.14159e5 flag=true'))
        self.assertEqual(h.kernel.public_action(314159.0),'••••')
        self.assertNotIn('true',h.kernel.public_action('token_is_true'))
        self.assertNotIn('314159',h.kernel.public_action('prefix314159suffix'))
        with self.assertRaises(ValueError): h.kernel.public_action({'true':1,'••••':2})
        self.assertNotIn('314159',h.kernel.actions.path.read_text())
        saved=json.loads(h.kernel.actions.path.read_text()); saved[run['id']]['redactionVersion']=4; saved[run['id']]['result']={'pin':314159}; h.kernel.actions.path.write_text(json.dumps(saved))
        from nachuan.actions import ActionRunner
        h.kernel.actions=ActionRunner(h.kernel,h.kernel.actions.path)
        self.assertNotIn('314159',h.kernel.actions.path.read_text())
        repeated=await h.run_action('grant',name='token',idempotencyKey='only-once')
        self.assertEqual(run['id'],repeated['id'])

    async def test_health_failure_has_no_secret_and_resets_ready(self):
        h=self.h; await h.add('token'); p=h.kernel.plugin('2api-plugin-token')
        await h.kernel.check_health(); self.assertTrue(p.health.ready)
        original=p.transport.call
        async def call(method,params,timeout):
            if method=='credential.health': raise PluginFailure(FailureKind.REAUTH,'unknown-sensitive-token')
            return await original(method,params,timeout)
        with patch.object(p.transport,'call',side_effect=call):
            response,body=await h.request('POST','/kernel/health/check')
        self.assertEqual(response.status,200); self.assertFalse(p.health.ready)
        self.assertNotIn('unknown-sensitive-token',json.dumps(body))
        await p.account_pool.save()
        self.assertNotIn('unknown-sensitive-token',(p.directory/'credentials/state.json').read_text())

    async def test_refresh_failure_does_not_skip_next_account(self):
        h=self.h; await h.add('token'); p=h.kernel.plugin('2api-plugin-token')
        await p.account_pool.add('two',{'apiKey':'another'})
        p.capabilities['credentialRefresh']=True
        for a in p.account_pool._accounts.values(): a.state=__import__('nachuan.accounts',fromlist=['AccountState']).AccountState.REAUTH_REQUIRED
        original=p.transport.call; calls=[]
        async def call(method,params,timeout):
            if method=='credential.refresh':
                calls.append(params['credential']['id'])
                if params['credential']['id']=='one': raise RuntimeError('refresh failure')
                return {'apiKey':'new-key'}
            return await original(method,params,timeout)
        with patch.object(p.transport,'call',side_effect=call): await h.kernel.refresh_credentials(p)
        self.assertEqual(calls,['one','two']); self.assertEqual(p.account_pool._accounts['two'].credential_generation,2)
