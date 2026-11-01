"""Regression coverage for the delivery review findings (local upstream only)."""
import asyncio
import json
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nachuan.app import create_app
from nachuan.config import HostConfig
from nachuan.contracts import PluginSetting, PluginUnavailable
from nachuan.kernel import Kernel
from nachuan.transport import ProcessTransport
from nachuan.streaming import StreamNormalizer
from test_kernel import write_plugin, RUNTIME


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def open_kernel(self, upstream):
        app = web.Application()
        async def chat(request):
            result = upstream(request)
            return await result if inspect.isawaitable(result) else result
        async def models(_):
            return web.json_response({'data':[{'id':'mock'}]})
        app.router.add_post('/chat', chat)
        app.router.add_get('/v1/models', models)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write_plugin(root / 'plugins', '2api-plugin-mock', base_url=str(server.make_url('')).rstrip('/'))
        config_path = root / 'config.json'
        raw = {'plugins':{'2api-plugin-mock':{'enabled':True,'maxInFlight':2}}}
        config_path.write_text(json.dumps(raw))
        kernel = Kernel(root / 'plugins', runtime_dir=RUNTIME)
        await kernel.configure(HostConfig.from_dict(raw))
        client = TestClient(TestServer(create_app(kernel, 'test-key', config_path)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return kernel, client, config_path

    async def test_status_health_config_roundtrip(self):
        kernel, client, path = await self.open_kernel(lambda _: web.json_response({'choices':[]}))
        headers = {'Authorization':'Bearer test-key'}
        status = await client.get('/kernel/status', headers=headers)
        self.assertEqual(status.status, 200)
        self.assertEqual((await status.json())['running'], 1)
        await kernel.check_health()
        self.assertTrue(kernel.list_plugins()[0]['health']['ready'])
        response = await client.get('/kernel/config', headers=headers)
        config = await response.json()
        config['plugins']['2api-plugin-mock'] = {'enabled':False,'maxInFlight':1}
        response = await client.put('/kernel/config', headers=headers, json=config)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())['config']['plugins']['2api-plugin-mock']['enabled'])
        self.assertIsNone(kernel.resolve('mock:mock'))
        self.assertEqual(HostConfig.from_dict(json.loads(path.read_text())).plugins['2api-plugin-mock'].max_in_flight, 1)

    async def test_http_limit_hot_update_and_413(self):
        _, client, _ = await self.open_kernel(lambda _: web.json_response({'choices':[]}))
        headers = {'Authorization':'Bearer test-key'}
        for limit, expected in [(1024,413),(8192,200)]:
            response = await client.put('/kernel/config', headers=headers, json={'maxRequestBytes':limit})
            self.assertEqual(response.status,200)
            response = await client.post('/v1/chat/completions',headers=headers,json={'model':'mock:mock','messages':[{'role':'user','content':'x'*2000}]})
            self.assertEqual(response.status,expected)
            await response.read()

    async def test_stream_error_is_failure_and_n2_is_preserved(self):
        async def upstream(request):
            body = await request.json()
            if body.get('n') == 2:
                value = {'choices':[{'index':i,'delta':{'content':text},'finish_reason':'stop'} for i,text in enumerate(('answer A','answer B'))]}
            else:
                value = {'error':{'code':'insufficient_quota','message':'quota exceeded'}}
            return web.Response(text='data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n',content_type='text/event-stream')
        kernel,client,_ = await self.open_kernel(upstream)
        headers = {'Authorization':'Bearer test-key'}
        response = await client.post('/v1/chat/completions',headers=headers,json={'model':'mock:mock','messages':[],'stream':True,'n':2})
        text = await response.text()
        self.assertIn('answer A',text)
        self.assertIn('answer B',text)
        self.assertIn('"index": 1',text)
        response = await client.post('/v1/chat/completions',headers=headers,json={'model':'mock:mock','messages':[],'stream':True})
        self.assertGreaterEqual(response.status,400)
        await response.read()
        self.assertEqual(kernel.monitor.snapshot()['totals']['successes'],1)
        self.assertEqual(kernel.monitor.snapshot()['totals']['failures'],1)

    async def test_large_rpc_then_small_rpc(self):
        script = "import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':'x'*r['params']['size']}),flush=True)"
        with tempfile.TemporaryDirectory() as tmp:
            transport = ProcessTransport([sys.executable,'-u','-c',script],tmp)
            self.addAsyncCleanup(transport.close)
            self.assertEqual(len(await transport.call('mock',{'size':70000},2)),70000)
            self.assertEqual(await transport.call('mock',{'size':1},2),'x')

    async def test_reload_keeps_plugin_and_account_limits(self):
        for plugin_limit in (1,2):
            gate, entered = asyncio.Event(), asyncio.Event()
            async def upstream(_):
                entered.set()
                await gate.wait()
                return web.json_response({'choices':[]})
            kernel,_,_ = await self.open_kernel(upstream)
            plugin,model=kernel.resolve('mock:mock')
            credential=plugin.directory/'credentials/default.json'
            credential.write_text(json.dumps({'apiKey':'fake','maxInFlight':1}))
            settings={'2api-plugin-mock':PluginSetting(max_in_flight=plugin_limit)}
            await kernel.load(settings)
            plugin,model=kernel.resolve('mock:mock')
            first=asyncio.create_task(kernel.invoke(plugin,{'messages':[]},model))
            try:
                await asyncio.wait_for(entered.wait(),2)
                await kernel.load(settings)
                new,new_model=kernel.resolve('mock:mock')
                with self.assertRaises(PluginUnavailable):
                    await kernel.invoke(new,{'messages':[]},new_model)
            finally:
                gate.set()
                await first
            await kernel.invoke(new,{'messages':[]},new_model)

    async def test_concurrent_account_attribution(self):
        gate, entered = asyncio.Event(), asyncio.Event()
        async def upstream(request):
            account=request.headers['Authorization'][7:]
            if account=='a':
                entered.set()
                await gate.wait()
            return web.json_response({'id':account,'usage':{'total_tokens':2}})
        kernel,_,_=await self.open_kernel(upstream)
        plugin,model=kernel.resolve('mock:mock')
        credentials=plugin.directory/'credentials'
        (credentials/'default.json').unlink()
        for uid in ('a','b'):
            (credentials/f'{uid}.json').write_text(json.dumps({'apiKey':uid,'maxInFlight':1}))
        await kernel.load()
        plugin,model=kernel.resolve('mock:mock')
        class First:
            def choice(self,values): return values[0]
        plugin.account_pool.random=First()
        a=asyncio.create_task(kernel.invoke(plugin,{'messages':[]},model))
        await asyncio.wait_for(entered.wait(),2)
        try:
            self.assertEqual((await kernel.invoke(plugin,{'messages':[]},model))['id'],'b')
        finally:
            gate.set()
            await a
        await asyncio.sleep(0)
        await kernel.ledger._queue.join()
        self.assertEqual(sorted(record['account'] for record in kernel.ledger.snapshot()['records']),['a','b'])

    def test_choices_have_independent_thought_state(self):
        normalizer=StreamNormalizer('mock')
        chunks=list(normalizer.data(json.dumps({'choices':[
            {'index':0,'delta':{'content':'<think>private'}},
            {'index':1,'delta':{'content':'public'}},
        ]})))
        self.assertEqual(chunks[0]['choices'][0]['delta'],{'reasoning_content':'private'})
        self.assertEqual(chunks[1]['choices'][0]['delta'],{'content':'public'})

    def test_ttft_snapshot(self):
        from nachuan.monitoring import Monitor
        import time
        monitor=Monitor()
        started=time.monotonic()-1
        request_id=monitor.start('mock','mock',True)
        monitor.finish('mock',request_id,started,model='mock',first_token_at=started+0.25)
        self.assertEqual(monitor.snapshot()['plugins']['mock']['average_ttft_ms'],250)

    async def test_oversize_protocol_message_isolates_transport(self):
        from unittest.mock import patch
        script = "import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':'x'*2000}),flush=True)"
        with tempfile.TemporaryDirectory() as tmp, patch('nachuan.transport.RPC_MAX_BYTES',1024):
            transport=ProcessTransport([sys.executable,'-u','-c',script],tmp)
            self.addAsyncCleanup(transport.close)
            with self.assertRaises(ValueError):
                await transport.call('mock',{},2)
            self.assertTrue(transport.closed)
            with self.assertRaises(RuntimeError):
                await transport.call('mock',{},0.1)
