"""WorkBuddy bridge integration against a local fake gateway."""
import asyncio
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from nachuan.app import create_app
from nachuan.kernel import Kernel

ROOT = Path(__file__).parents[2]
RUNTIME = ROOT / 'plugin-runtime'
PLUGIN = ROOT / 'plugins' / '2api-plugin-workbuddy'


class WorkBuddyBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_model_mapping_and_stream(self):
        calls = []
        model_catalog = ['cn:glm-5.2']

        async def models(request):
            calls.append(('models', request.headers.get('Authorization')))
            if request.headers.get('Authorization') != 'Bearer test-gateway-key':
                return web.json_response({'error': {'message': 'unauthorized'}}, status=401)
            return web.json_response({'data': [{'id': name} for name in model_catalog]})

        async def status(request):
            calls.append(('status', request.headers.get('Authorization')))
            return web.json_response({'accounts': [
                {'uid': 'one', 'credits': 75, 'credits_total': 100, 'disabled': False, 'cooling': False, 'access_token': 'should-not-leak'},
                {'uid': 'two', 'credits': 0, 'credits_total': 0, 'disabled': True, 'cooling': False},
            ], 'secret': 'should-not-leak'})

        async def chat(request):
            body = await request.json()
            calls.append(('chat', request.headers.get('Authorization'), body['model'], body['stream']))
            usage = {'total_tokens': 7, 'prompt_tokens': 3, 'completion_tokens': 4, 'vendor': {'cost': 999, 'unit': 'forged'}}
            if 'test_credit' in body:
                usage['credit'] = body['test_credit']
            if not body['stream']:
                return web.json_response({'id': 'completion', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}], 'usage': usage})
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            chunk = {'choices': [{'index': 0, 'delta': {'content': 'ok'}}], 'usage': usage}
            await response.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            await response.write(b'data: [DONE]\n\n')
            return response

        upstream_app = web.Application()
        upstream_app.router.add_get('/v1/models', models)
        upstream_app.router.add_get('/status', status)
        upstream_app.router.add_post('/v1/chat/completions', chat)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        self.addAsyncCleanup(upstream.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name) / 'plugins' / PLUGIN.name
        directory.mkdir(parents=True)
        spec = json.loads((PLUGIN / 'spec.json').read_text())
        self.assertEqual(spec['config']['baseUrl'], 'http://127.0.0.1:7863')
        spec['config']['baseUrl'] = str(upstream.make_url('')).rstrip('/')
        (directory / 'spec.json').write_text(json.dumps(spec))
        code = (PLUGIN / 'index.ts').read_text().replace('../../plugin-runtime/dist/openai.js', (RUNTIME / 'dist/openai.js').as_uri()).replace('../../plugin-runtime/dist/contracts.js', (RUNTIME / 'dist/contracts.js').as_uri())
        (directory / 'index.ts').write_text(code)
        credentials = directory / 'credentials'
        credentials.mkdir()
        kernel = Kernel(directory.parent, runtime_dir=RUNTIME)
        await kernel.load()
        client = TestClient(TestServer(create_app(kernel, 'local-key')))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        headers = {'Authorization': 'Bearer local-key'}
        response = await client.post('/kernel/plugins/2api-plugin-workbuddy/accounts', headers=headers, json={'id': 'gateway', 'apiKey': 'test-gateway-key'})
        self.assertEqual(response.status, 201)
        response = await client.get('/v1/models', headers=headers)
        self.assertIn('workbuddy:glm-5.2', [model['id'] for model in (await response.json())['data']])
        plugin, _ = kernel.resolve('workbuddy:glm-5.2')
        health = await plugin.transport.call('credential.health', {'credential': {'id': 'gateway', 'secret': {'apiKey': 'test-gateway-key'}}}, 5)
        self.assertTrue(health['modelsReady'])
        with self.assertRaises(Exception):
            await plugin.transport.call('credential.health', {'credential': {'id': 'gateway', 'secret': {'apiKey': 'wrong-key'}}}, 5)
        for stream in (False, True):
            response = await client.post('/v1/chat/completions', headers=headers, json={'model': 'workbuddy:glm-5.2', 'messages': [], 'stream': stream})
            self.assertEqual(response.status, 200)
            if stream:
                result = await response.text()
                self.assertIn('ok', result)
                self.assertIn('[DONE]', result)
            else:
                self.assertEqual((await response.json())['id'], 'completion')
        self.assertEqual(calls, [('models', 'Bearer test-gateway-key'), ('models', 'Bearer wrong-key'), ('chat', 'Bearer test-gateway-key', 'cn:glm-5.2', False), ('chat', 'Bearer test-gateway-key', 'cn:glm-5.2', True)])

        model_catalog[:] = ['unrelated-model']
        health = await plugin.transport.call('credential.health', {'credential': {'id': 'gateway', 'secret': {'apiKey': 'test-gateway-key'}}}, 5)
        self.assertFalse(health['modelsReady'])
        model_catalog.clear()
        health = await plugin.transport.call('credential.health', {'credential': {'id': 'gateway', 'secret': {'apiKey': 'test-gateway-key'}}}, 5)
        self.assertFalse(health['modelsReady'])

        # The upstream pool is queried only upon explicit action, and fields are projected.
        response = await client.post('/kernel/plugins/2api-plugin-workbuddy/actions/gateway_accounts/runs', headers=headers, json={'accountId': 'gateway', 'input': {}})
        self.assertEqual(response.status, 202)
        run = await kernel.actions.wait((await response.json())['id'])
        self.assertEqual(run['state'], 'succeeded', run)
        result = run['result']
        self.assertEqual(result['source'], 'gateway_cache')
        self.assertEqual(result['accounts'][0]['cachedCredits'], 75)
        self.assertEqual(result['accounts'][0]['cachedCreditsTotal'], 100)
        self.assertEqual(result['accounts'][1]['cachedCredits'], 0)
        self.assertNotIn('cachedCreditsTotal', result['accounts'][1])
        self.assertNotIn('paused', result['accounts'][1])
        self.assertNotIn('should-not-leak', json.dumps(result))
        self.assertNotIn('test-gateway-key', json.dumps(result))
        self.assertEqual(calls[-1], ('status', 'Bearer test-gateway-key'))
        response = await client.get('/kernel/plugins/2api-plugin-workbuddy/accounts/gateway/quota', headers=headers)
        self.assertEqual((await response.json())['state'], 'unsupported')

        # Valid zero is measured; missing, negative and wrong-type credit stay unknown.
        for stream in (False, True):
            for credit in (1.25, 0, -1, '2', None):
                response = await client.post('/v1/chat/completions', headers=headers, json={'model': 'workbuddy:glm-5.2', 'messages': [], 'stream': stream, 'test_credit': credit})
                self.assertEqual(response.status, 200)
                if stream:
                    text = await response.text()
                    chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ') and line != 'data: [DONE]']
                    usage = [chunk['usage'] for chunk in chunks if isinstance(chunk.get('usage'), dict)][-1]
                else:
                    usage = (await response.json())['usage']
                if type(credit) in (int, float) and credit >= 0:
                    self.assertEqual(usage.get('vendor'), {'cost': credit, 'unit': 'workbuddy_credit', 'dimension': 'chat'})
                else:
                    self.assertNotIn('vendor', usage)
        await asyncio.sleep(0.1)
        await kernel.ledger._queue.join()
        settlements = kernel.ledger.snapshot(plugin='2api-plugin-workbuddy')['settlements']
        self.assertEqual(len(settlements), 1)
        self.assertEqual((settlements[0]['plugin'], settlements[0]['unit'], settlements[0]['dimension']), ('2api-plugin-workbuddy', 'workbuddy_credit', 'chat'))
        self.assertEqual(Decimal(settlements[0]['cost']), Decimal('2.5'))
