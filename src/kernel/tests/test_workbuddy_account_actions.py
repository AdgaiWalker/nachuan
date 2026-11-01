"""WorkBuddy gateway account actions against a local fake service only."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from nachuan.app import create_app
from nachuan.kernel import Kernel

ROOT = Path(__file__).parents[2]
RUNTIME = ROOT / 'plugin-runtime'
PLUGIN = ROOT / 'plugins' / '2api-plugin-workbuddy'


class WorkBuddyAccountActionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

        async def account_action(request):
            uid = request.match_info['uid']
            op = request.match_info['op']
            self.calls.append((uid, op, request.headers.get('Authorization')))
            if uid == 'http-error':
                return web.json_response({'error': 'private-upstream-error'}, status=404)
            if uid == 'unconfirmed':
                return web.json_response({'ok': False, 'private': 'private-upstream-error'})
            if uid == 'malformed':
                return web.Response(text='private-upstream-error')
            if uid in {'wait-timeout', 'wait-cancel'}:
                self.entered.set()
                await self.release.wait()
            return web.json_response({'ok': True, 'private': 'private-upstream-error'})

        app = web.Application()
        app.router.add_post('/panel/api/accounts/{uid}/{op}', account_action)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / 'plugins' / PLUGIN.name
        self.directory.mkdir(parents=True)
        spec = json.loads((PLUGIN / 'spec.json').read_text())
        spec['config']['baseUrl'] = str(server.make_url('')).rstrip('/')
        # Keep the regression fast; the actual published timeout remains unchanged.
        for action in spec['actions']:
            if action['id'].startswith('gateway_account_'):
                action['timeoutSeconds'] = 1
        (self.directory / 'spec.json').write_text(json.dumps(spec))
        code = (PLUGIN / 'index.ts').read_text().replace('../../plugin-runtime/dist/openai.js', (RUNTIME / 'dist/openai.js').as_uri()).replace('../../plugin-runtime/dist/contracts.js', (RUNTIME / 'dist/contracts.js').as_uri())
        (self.directory / 'index.ts').write_text(code)
        (self.directory / 'credentials').mkdir()
        (self.directory / 'credentials' / 'gateway.json').write_text(json.dumps({'apiKey': 'fake-gateway-key'}))
        self.kernel = Kernel(self.directory.parent, runtime_dir=RUNTIME)
        await self.kernel.load()
        self.client = TestClient(TestServer(create_app(self.kernel, 'local-key')))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {'Authorization': 'Bearer local-key'}

    async def asyncTearDown(self):
        self.release.set()

    async def start(self, operation, uid, key):
        response = await self.client.post(f'/kernel/plugins/2api-plugin-workbuddy/actions/gateway_account_{operation}/runs', headers=self.headers, json={
            'accountId': 'gateway', 'input': {'uid': uid}, 'idempotencyKey': key,
        })
        return response.status, await response.json()

    async def test_manual_actions_are_scoped_and_idempotent(self):
        missing_key = await self.client.post('/kernel/plugins/2api-plugin-workbuddy/actions/gateway_account_pause/runs', headers=self.headers, json={'accountId': 'gateway', 'input': {'uid': 'user_1-2'}})
        self.assertEqual(missing_key.status, 400)
        for operation in ('pause', 'resume'):
            status, queued = await self.start(operation, 'user_1-2', operation + '-once')
            self.assertEqual(status, 202)
            run = await self.kernel.actions.wait(queued['id'])
            self.assertEqual(run['state'], 'succeeded')
            self.assertEqual(run['result'], {'ok': True})
            status, again = await self.start(operation, 'user_1-2', operation + '-once')
            self.assertEqual(status, 202)
            self.assertEqual(again['id'], run['id'])
            status, _ = await self.start(operation, 'different', operation + '-once')
            self.assertEqual(status, 409)
        self.assertEqual(self.calls, [
            ('user_1-2', 'pause', 'Bearer fake-gateway-key'),
            ('user_1-2', 'resume', 'Bearer fake-gateway-key'),
        ])
        history = self.kernel.actions.path.read_text()
        self.assertNotIn('fake-gateway-key', history)
        self.assertNotIn('private-upstream-error', history)

    async def test_invalid_uid_never_reaches_gateway(self):
        for i, uid in enumerate(('.', '..', '../pause', '%2e%2e', 'a/b', 'a?b', '中文', 'x' * 65)):
            status, data = await self.start('pause', uid, f'invalid-{i}')
            if status == 202:
                run = await self.kernel.actions.wait(data['id'])
                self.assertNotEqual(run['state'], 'succeeded')
            else:
                self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])

    async def test_failed_requests_are_unknown_and_not_replayed(self):
        for uid in ('http-error', 'unconfirmed', 'malformed'):
            status, queued = await self.start('pause', uid, 'failure-' + uid)
            self.assertEqual(status, 202)
            run = await self.kernel.actions.wait(queued['id'])
            self.assertEqual(run['state'], 'unknown')
            self.assertNotIn('private-upstream-error', json.dumps(run))
            _, again = await self.start('pause', uid, 'failure-' + uid)
            self.assertEqual(again['id'], run['id'])
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn('private-upstream-error', self.kernel.actions.path.read_text())

    async def test_timeout_and_cancel_do_not_replay(self):
        status, queued = await self.start('pause', 'wait-timeout', 'timeout-once')
        self.assertEqual(status, 202)
        await asyncio.wait_for(self.entered.wait(), 2)
        status, _ = await self.start('resume', 'wait-timeout', 'concurrent-opposite')
        self.assertEqual(status, 409)
        run = await self.kernel.actions.wait(queued['id'])
        self.assertEqual(run['state'], 'unknown')
        _, again = await self.start('pause', 'wait-timeout', 'timeout-once')
        self.assertEqual(again['id'], run['id'])
        self.assertEqual(len(self.calls), 1)
        self.entered.clear()
        status, queued = await self.start('resume', 'wait-cancel', 'cancel-once')
        self.assertEqual(status, 202)
        await asyncio.wait_for(self.entered.wait(), 2)
        response = await self.client.post('/kernel/action-runs/' + queued['id'] + '/cancel', headers=self.headers)
        self.assertEqual((await response.json())['state'], 'unknown')
        _, again = await self.start('resume', 'wait-cancel', 'cancel-once')
        self.assertEqual(again['id'], queued['id'])
        self.assertEqual(len(self.calls), 2)
