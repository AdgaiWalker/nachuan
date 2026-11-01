import asyncio
import json
import tempfile
import unittest
import copy
from pathlib import Path
from nachuan.accounts import AccountPool, Failure, FailureKind, AccountState
from nachuan.actions import ActionRunner
from nachuan.contracts import validate_spec, ContractError
from nachuan.schema import protect, validate
from nachuan.transport import ProcessTransport
import test_core_v2 as base


class ReviewV21(unittest.IsolatedAsyncioTestCase):
    def test_anyof_annotations_are_conservative(self):
        schema={'type':'object','anyOf':[{'type':'object','properties':{'items':{'type':'array','items':{'type':'object','properties':{'n':{'type':'integer','secret':True}}}}}}, {'type':'object','properties':{'other':{'type':'string','secret':True}}}]}
        value={'items':[{'n':123,'ok':'visible'}],'other':'private'}
        validate(value,schema)
        self.assertEqual(protect(value,schema),{'items':[{'n':'••••','ok':'visible'}],'other':'••••'})

    def test_v3_history_is_scrubbed_preserving_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'runs.json'
            path.write_text(json.dumps({'r':{'id':'r','plugin':'p','action':'a','fingerprint':'f','state':'succeeded','sideEffect':True,'idempotencyHash':'a'*64,'redactionVersion':3,'result':{'private':'sensitive'}}}))
            runner=ActionRunner(None,path)
            self.assertEqual(runner.runs['r']['idempotencyHash'],'a'*64)
            self.assertNotIn('sensitive',path.read_text())
            self.assertEqual(runner.runs['r']['redactionVersion'],5)

    def test_bad_runtime_is_contract_error(self):
        with self.assertRaises(ContractError):
            validate_spec({'name':'plugin-a','version':'1','runtime':[]},'.')

    async def test_rotation_fences_old_failure_and_bad_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool=AccountPool(tmp)
            await pool.add('a',{'apiKey':'old'})
            lease=await pool.acquire()
            await pool.rotate('a',{'apiKey':'new'},replace=True,recovered=True)
            await lease.release(Failure(FailureKind.REAUTH))
            self.assertEqual(pool._accounts['a'].state,AccountState.READY)
            await pool.add('b',{'apiKey':'other'})
            pool._accounts['a'].credential_file.unlink()
            lease=await pool.acquire()
            self.assertEqual(lease.account.id,'b')
            await lease.release(None)
            await pool.close()

    async def test_full_stream_queue_does_not_block_rpc(self):
        import sys
        script='''import sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n if r.get("method")=="stream":\n  for i in range(100): print(json.dumps({"id":r["id"],"method":"chunk","params":str(i)}),flush=True)\n elif r.get("id"): print(json.dumps({"id":r["id"],"result":"ok"}),flush=True)\n'''
        with tempfile.TemporaryDirectory() as tmp:
            t=ProcessTransport([sys.executable,'-u','-c',script],tmp)
            try:
                stream=t.stream('stream',{},2)
                await anext(stream)
                await asyncio.sleep(.1)
                self.assertEqual(await t.call('ping',{},1),'ok')
                await stream.aclose()
                self.assertEqual(await t.call('ping',{},1),'ok')
            finally: await t.close()


class IntegrationReview(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=base.CoreV2Tests()
        await self.h.asyncSetUp()

    async def asyncTearDown(self):
        self.h.gate.set()
        await self.h.client.close()
        await self.h.upstream.close()
        self.h.tmp.cleanup()

    async def test_anyof_action_result_is_redacted_in_http_and_disk(self):
        h=self.h
        await h.add('token')
        spec=copy.deepcopy(h.specs['token'])
        spec['actions'][0]['outputSchema']={'type':'object','anyOf':[{'type':'object','properties':{'privateReport':{'type':'object','secret':True}},'required':['privateReport']}]}
        p=h.kernel.plugin('2api-plugin-token')
        (p.directory/'spec.json').write_text(json.dumps(spec))
        await h.kernel.load()
        run=await h.run_action('probe',name='token')
        response,body=await h.request('GET','/kernel/action-runs/'+run['id'])
        self.assertEqual(response.status,200)
        self.assertEqual(body['result']['privateReport'],'••••')
        self.assertNotIn('private-action-value',h.kernel.actions.path.read_text())

    async def test_quota_refresh_continues_without_health_handler(self):
        h=self.h
        p=h.kernel.plugin('2api-plugin-token'); entry=p.directory/'index.ts'
        entry.write_text(entry.read_text().replace('healthCheck:async()=>({reachable:true,authenticated:true,modelsReady:true,accountReady:true}),',''))
        await h.kernel.load(); await h.add('token')
        p=h.kernel.plugin('2api-plugin-token')
        before=h.quota_hits
        await h.kernel.check_health()
        self.assertGreater(h.quota_hits,before)
        self.assertEqual(h.kernel.quota.get(p,'one')['state'],'known')
