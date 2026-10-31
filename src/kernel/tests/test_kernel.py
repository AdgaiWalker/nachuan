import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nachuan.app import create_app
from nachuan.config import HostConfig
from nachuan.contracts import Lifecycle, PluginSetting, PluginUnavailable
from nachuan.kernel import Kernel

ROOT = Path(__file__).parents[1]
RUNTIME = ROOT.parent / "plugin-runtime"


def write_plugin(root: Path, name: str, model: str = "mock", base_url: str = "http://127.0.0.1:1") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    runtime_url = (RUNTIME / "dist" / "openai.js").as_uri()
    (directory / "index.ts").write_text(f"import {{ openAIBusiness }} from '{runtime_url}'; export default openAIBusiness([{{id:'{model}'}}]);", encoding="utf-8")
    spec = {"name": name, "version": "1.0.0", "runtime": {"entry": "index.ts"}, "models": [{"id": model, "upstreamId": "upstream-mock"}], "config": {"baseUrl": base_url, "chatPath": "/chat", "apiKey": "upstream-secret"}}
    (directory / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    credentials = directory / "credentials"
    credentials.mkdir()
    (credentials / "default.json").write_text(json.dumps({"apiKey": "upstream-secret"}), encoding="utf-8")
    return directory


class KernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_process_handshake_and_completion(self):
        async def upstream(request):
            body = await request.json()
            self.assertEqual(request.headers["Authorization"], "Bearer upstream-secret")
            if body["stream"]:
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
                await response.write(b'data: {"choices":[{"delta":{"content":"<th"}}]}\n\n')
                await response.write(b'data: {"choices":[{"delta":{"content":"ink>thinking</think>done"}}],"usage":{"total_tokens":7}}\n\n')
                await response.write(b"data: [DONE]\n\n")
                return response
            self.assertEqual(body["model"], "upstream-mock")
            return web.json_response({"id": "real-completion", "object": "chat.completion", "choices": []})

        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        server = TestServer(upstream_app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as temp:
            write_plugin(Path(temp), "2api-plugin-mock", base_url=str(server.make_url("")).rstrip("/"))
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            settings = {"2api-plugin-mock": PluginSetting(max_in_flight=1)}
            await kernel.load(settings)
            app = create_app(kernel, "gateway-secret")
            client = TestClient(TestServer(app))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            models = await client.get("/v1/models", headers={"Authorization": "Bearer gateway-secret"})
            self.assertEqual(models.status, 200)
            self.assertEqual((await models.json())["data"][0]["id"], "mock:mock")
            response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer gateway-secret"}, json={"model": "mock:mock", "messages": [], "stream": False})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["id"], "real-completion")
            response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer gateway-secret"}, json={"model": "mock:mock", "messages": [], "stream": True})
            self.assertEqual(response.status, 200, await response.text())
            text = await response.text()
            self.assertIn("reasoning_content", text)
            self.assertIn("done", text)
            self.assertIn("[DONE]", text)
            self.assertEqual(kernel.monitor.snapshot()["totals"]["tokens"], 7)
            self.assertGreater(kernel.monitor.snapshot()["plugins"]["2api-plugin-mock"]["ttft_samples"], 0)

    async def test_disable_and_conflicting_bare_model(self):
        with tempfile.TemporaryDirectory() as temp:
            write_plugin(Path(temp), "2api-plugin-a")
            write_plugin(Path(temp), "2api-plugin-b")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load({"2api-plugin-b": PluginSetting(enabled=False)})
            self.assertIsNotNone(kernel.resolve("mock"))
            self.assertIsNone(kernel.resolve("b:mock"))
            await kernel.load({"2api-plugin-b": PluginSetting(enabled=True)})
            self.assertIsNone(kernel.resolve("mock"))
            self.assertIsNotNone(kernel.resolve("a:mock"))
            self.assertIsNotNone(kernel.resolve("b:mock"))

    async def test_bad_spec_keeps_last_good(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = write_plugin(Path(temp), "2api-plugin-mock")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load()
            (directory / "spec.json").write_text("{", encoding="utf-8")
            await kernel.load()
            self.assertEqual(kernel.status.faults, 1)
            self.assertIsNotNone(kernel.resolve("mock:mock"))
            self.assertEqual(kernel.list_plugins()[0]["status"], Lifecycle.RUNNING.value)

    async def test_account_failover_uses_next_credential(self):
        seen = []
        async def upstream(request):
            seen.append(request.headers["Authorization"])
            if len(seen) == 1:
                return web.json_response({"error": "limited"}, status=429)
            return web.json_response({"id": "second-account", "choices": []})
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        server = TestServer(upstream_app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as temp:
            plugin_dir = write_plugin(Path(temp), "2api-plugin-mock", base_url=str(server.make_url("")).rstrip("/"))
            credentials = plugin_dir / "credentials"
            credentials.mkdir(exist_ok=True)
            for uid in ("first", "second"):
                (credentials / f"{uid}.json").write_text(json.dumps({"apiKey": uid}), encoding="utf-8")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load()
            route = kernel.resolve("mock:mock")
            result = await kernel.invoke(route[0], {"model": "mock:mock", "messages": []}, route[1])
            self.assertEqual(result["id"], "second-account")
            self.assertEqual(len(seen), 2)
            self.assertNotEqual(seen[0], seen[1])

    async def test_crashed_plugin_is_removed_from_routing(self):
        with tempfile.TemporaryDirectory() as temp:
            write_plugin(Path(temp), "2api-plugin-mock")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load()
            plugin = kernel.resolve("mock:mock")[0]
            plugin.transport.process.terminate()
            await plugin.transport.process.wait()
            await asyncio.sleep(0.02)
            self.assertIsNone(kernel.resolve("mock:mock"))
            with self.assertRaises(PluginUnavailable):
                await kernel.invoke(plugin, {"model": "mock:mock", "messages": []}, kernel._snapshot.models["mock:mock"][1])

    async def test_reload_recovers_crashed_plugin(self):
        with tempfile.TemporaryDirectory() as temp:
            write_plugin(Path(temp), "2api-plugin-mock")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load()
            plugin = kernel.resolve("mock:mock")[0]
            await plugin.transport.close()
            await asyncio.sleep(0.01)
            self.assertIsNone(kernel.resolve("mock:mock"))
            await kernel.load()
            self.assertIsNotNone(kernel.resolve("mock:mock"))
            self.assertEqual(kernel.status.faults, 0)

    async def test_explicit_cross_plugin_fallback(self):
        calls = []
        async def upstream(request):
            authorization = request.headers.get("Authorization", "")
            calls.append(authorization)
            if authorization.endswith("upstream-secret") and not any(call.endswith("b") for call in calls[:-1]):
                return web.json_response({"error": "unavailable"}, status=503)
            return web.json_response({"id": "fallback-success", "choices": []})
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        server = TestServer(upstream_app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as temp:
            for name in ("2api-plugin-a", "2api-plugin-b"):
                directory = write_plugin(Path(temp), name, model=name[-1], base_url=str(server.make_url("")).rstrip("/"))
                if name.endswith("b"):
                    (directory / "credentials" / "default.json").write_text(json.dumps({"apiKey": "b"}), encoding="utf-8")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.configure(HostConfig(fallback_enabled=True, routes={"alias": ("a", "b")}))
            client = TestClient(TestServer(create_app(kernel, "secret")))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret"}, json={"model": "alias", "messages": []})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(response.headers["X-Nachuan-Model"], "b")
            self.assertEqual((await response.json())["id"], "fallback-success")

    async def test_cancelled_invocation_releases_account_and_permit(self):
        release = asyncio.Event()
        async def upstream(_):
            release.set()
            await asyncio.sleep(30)
            return web.json_response({})
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        server = TestServer(upstream_app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as temp:
            directory = write_plugin(Path(temp), "2api-plugin-mock", base_url=str(server.make_url("")).rstrip("/"))
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load({"2api-plugin-mock": PluginSetting(max_in_flight=1)})
            plugin, model = kernel.resolve("mock:mock")
            task = asyncio.create_task(kernel.invoke(plugin, {"model": "mock:mock", "messages": []}, model))
            await asyncio.wait_for(release.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(plugin.in_flight, 0)
            self.assertEqual(plugin.account_pool._accounts["default"].in_flight, 0)

    async def test_cancelled_stream_releases_account_and_permit(self):
        with tempfile.TemporaryDirectory() as temp:
            write_plugin(Path(temp), "2api-plugin-mock")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load({"2api-plugin-mock": PluginSetting(max_in_flight=1)})
            plugin, model = kernel.resolve("mock:mock")
            async def endless(*_):
                yield json.dumps({"choices": [{"delta": {"content": "x"}}]})
                await asyncio.sleep(30)
            plugin.transport.stream = endless
            stream = kernel.stream(plugin, {"model": "mock:mock", "messages": []}, model)
            self.assertIn("x", await anext(stream))
            await stream.aclose()
            self.assertEqual(plugin.in_flight, 0)
            self.assertEqual(plugin.account_pool._accounts["default"].in_flight, 0)

    async def test_stream_failure_before_first_chunk_is_http_error(self):
        async def upstream(_):
            return web.json_response({"error": "unavailable"}, status=503)
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat", upstream)
        server = TestServer(upstream_app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as temp:
            write_plugin(Path(temp), "2api-plugin-mock", base_url=str(server.make_url("")).rstrip("/"))
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.load()
            client = TestClient(TestServer(create_app(kernel, "secret")))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret"}, json={"model": "mock:mock", "messages": [], "stream": True})
            self.assertEqual(response.status, 503)
            self.assertEqual(response.content_type, "application/json")
            self.assertEqual((await response.json())["error"]["code"], "plugin_unavailable")

    async def test_disabled_plugin_keeps_last_good_on_bad_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = write_plugin(Path(temp), "2api-plugin-mock")
            kernel = Kernel(temp, runtime_dir=RUNTIME)
            self.addAsyncCleanup(kernel.close)
            await kernel.configure(HostConfig(plugins={"2api-plugin-mock": PluginSetting(enabled=True)}))
            await kernel.configure(HostConfig(plugins={"2api-plugin-mock": PluginSetting(enabled=False)}))
            self.assertEqual(kernel.list_plugins()[0]["status"], "disabled")
            (directory / "spec.json").write_text("{", encoding="utf-8")
            await kernel.configure(HostConfig(plugins={"2api-plugin-mock": PluginSetting(enabled=True)}))
            self.assertEqual(kernel.list_plugins()[0]["status"], "disabled")

    async def test_real_plugin_specs(self):
        kernel = Kernel(ROOT.parent / "plugins", runtime_dir=RUNTIME)
        self.addAsyncCleanup(kernel.close)
        await kernel.load()
        self.assertEqual(kernel.status.running, 0)
        self.assertEqual(kernel.status.faults, 0)
        self.assertEqual(len(kernel.list_models()), 0)
        self.assertTrue(all(item["status"] == "credential_missing" for item in kernel.list_plugins()))


if __name__ == "__main__":
    unittest.main()
