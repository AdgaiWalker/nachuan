"""Per-plugin diagnostics do not trigger unrelated account maintenance."""
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nachuan.app import create_app
from nachuan.kernel import Kernel
from test_kernel import RUNTIME, write_plugin


class PluginHealthApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_targets_only_the_selected_plugin(self):
        calls = []

        async def models(request):
            calls.append(request.headers.get('Authorization'))
            if calls[-1] == 'Bearer broken-secret':
                return web.json_response({'error': 'broken-secret'}, status=401)
            return web.json_response({'data': [{'id': 'mock'}]})

        upstream_app = web.Application()
        upstream_app.router.add_get('/v1/models', models)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        self.addAsyncCleanup(upstream.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ('2api-plugin-a', '2api-plugin-b'):
            directory = write_plugin(root, name, model=name[-1], base_url=str(upstream.make_url('')).rstrip('/'))
            key = 'working-secret' if name.endswith('a') else 'broken-secret'
            (directory / 'credentials' / 'default.json').write_text(json.dumps({'apiKey': key}))
        kernel = Kernel(root, runtime_dir=RUNTIME)
        await kernel.load()
        client = TestClient(TestServer(create_app(kernel, 'local-key')))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        headers = {'Authorization': 'Bearer local-key'}

        response = await client.post('/kernel/plugins/2api-plugin-a/health/check', headers=headers)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['health']['ready'])
        self.assertEqual(calls, ['Bearer working-secret'])

        response = await client.post('/kernel/plugins/2api-plugin-b/health/check', headers=headers)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertFalse(json.loads(text)['health']['ready'])
        self.assertNotIn('broken-secret', text)
        self.assertEqual(calls, ['Bearer working-secret', 'Bearer broken-secret'])

        response = await client.post('/kernel/plugins/unknown/health/check', headers=headers)
        self.assertEqual(response.status, 404)
        response = await client.post('/kernel/plugins/2api-plugin-a/health/check')
        self.assertEqual(response.status, 401)
