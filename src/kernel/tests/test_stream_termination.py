"""End-to-end regression for truncated OpenAI-compatible SSE streams."""
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from nachuan.app import create_app
from nachuan.kernel import Kernel
from test_kernel import RUNTIME, write_plugin


class StreamTerminationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_eof_before_done_is_not_success(self):
        chunk = 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'partial'}}]}) + '\n\n'

        async def upstream(request):
            body = await request.json()
            mode = body['messages'][0]['content']
            if mode == 'empty':
                text = ''
            elif mode == 'truncated':
                text = chunk
            else:
                text = chunk + 'data: [DONE]\n\n'
            return web.Response(text=text, content_type='text/event-stream')

        upstream_app = web.Application()
        upstream_app.router.add_post('/chat', upstream)
        server = TestServer(upstream_app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        write_plugin(Path(temp.name), '2api-plugin-mock', base_url=str(server.make_url('')).rstrip('/'))
        kernel = Kernel(temp.name, runtime_dir=RUNTIME)
        await kernel.load()
        client = TestClient(TestServer(create_app(kernel, 'gateway-key')))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        headers = {'Authorization': 'Bearer gateway-key'}

        async def call(mode):
            return await client.post('/v1/chat/completions', headers=headers, json={
                'model': 'mock:mock', 'messages': [{'role': 'user', 'content': mode}], 'stream': True,
            })

        complete = await call('complete')
        self.assertEqual(complete.status, 200)
        data = await complete.text()
        self.assertIn('partial', data)
        self.assertIn('data: [DONE]', data)
        self.assertNotIn('"error"', data)

        empty = await call('empty')
        self.assertGreaterEqual(empty.status, 400)
        self.assertEqual(empty.content_type, 'application/json')
        self.assertIn('error', await empty.json())

        truncated = await call('truncated')
        self.assertEqual(truncated.status, 200)
        text = await truncated.text()
        self.assertIn('partial', text)
        self.assertIn('"type": "stream_error"', text)
        self.assertIn('data: [DONE]', text)
        totals = kernel.monitor.snapshot()['totals']
        self.assertEqual(totals['successes'], 1)
        self.assertEqual(totals['failures'], 2)
