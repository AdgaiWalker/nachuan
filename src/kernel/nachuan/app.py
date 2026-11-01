"""Python OpenAI 兼容宿主；不含 Web 管理界面与具体平台逻辑。"""
from __future__ import annotations

from dataclasses import asdict
import argparse
import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any
import os
import secrets
import time

from aiohttp import web
from .contracts import PluginSetting, PluginUnavailable
from .errors import PluginFailure
from .monitoring import Monitor
from .kernel import Kernel
from .config import ConfigStore, HostConfig
from .tasks import TaskSpec
from .control import routes as control_routes, errors as control_errors


KERNEL_KEY = web.AppKey("kernel", object)
MONITOR_KEY = web.AppKey("monitor", Monitor)
CONFIG_KEY = web.AppKey("config", ConfigStore)
STREAM_TICKETS: dict[str, float] = {}


def create_app(kernel: Kernel, api_key: str = "", config_path: str | Path | None = None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware(api_key),control_errors], client_max_size=kernel.config.max_request_bytes)
    app.on_cleanup.append(lambda app: kernel.close())
    app.router.add_get("/health", health)
    app.router.add_get("/kernel/monitor", monitor_snapshot)
    app.router.add_get("/kernel/ledger", ledger_snapshot)
    app.router.add_get("/kernel/events", events)
    app.router.add_post("/kernel/events/ticket", event_ticket)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/kernel/status", status)
    app.router.add_get("/kernel/plugins", plugins)
    control_routes(app,KERNEL_KEY)
    app.router.add_post("/kernel/reload", reload_kernel)
    app.router.add_post("/kernel/health/check", check_health)
    app.router.add_get("/kernel/tasks", tasks)
    app.router.add_post("/kernel/tasks/{id}/run", run_task)
    app.router.add_get("/kernel/config", get_config)
    app.router.add_put("/kernel/config", put_config)
    app[KERNEL_KEY] = kernel
    app[MONITOR_KEY] = kernel.monitor
    app[CONFIG_KEY] = ConfigStore(config_path or kernel.plugins_dir.parent / "backend" / "config.local.json")
    return app


def auth_middleware(api_key: str):
    @web.middleware
    async def authenticate(request: web.Request, handler):
        # Each new request uses the live limit, including after a hot update.
        request._client_max_size = request.app[KERNEL_KEY].config.max_request_bytes
        if api_key and request.path != "/health":
            authorization = request.headers.get("Authorization", "")
            ticket = request.query.get("ticket", "")
            ticket_valid = bool(ticket) and STREAM_TICKETS.get(ticket, 0) > time.time()
            if ticket_valid:
                STREAM_TICKETS.pop(ticket, None)
            if not ticket_valid and (not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization[7:], api_key)):
                return web.json_response({"error": {"message": "invalid API key", "type": "invalid_request_error"}}, status=401)
        return await handler(request)
    return authenticate


async def health(request: web.Request) -> web.Response:
    kernel: Kernel = request.app[KERNEL_KEY]
    return web.json_response({"ok": True, "generation": kernel.status.generation, "faults": kernel.status.faults})


async def monitor_snapshot(request: web.Request) -> web.Response:
    data = request.app[MONITOR_KEY].snapshot()
    plugin = request.query.get("plugin")
    if plugin:
        data["events"] = [event for event in data["events"] if event["plugin"] == plugin]
    level = request.query.get("level")
    if level:
        data["events"] = [event for event in data["events"] if event["level"] == level]
    return web.json_response(data)


async def event_ticket(request: web.Request) -> web.Response:
    ticket = secrets.token_urlsafe(24)
    now = time.time()
    for key, expiry in list(STREAM_TICKETS.items()):
        if expiry <= now:
            STREAM_TICKETS.pop(key, None)
    STREAM_TICKETS[ticket] = now + 30
    return web.json_response({"ticket": ticket, "expires_in": 30})


async def events(request: web.Request) -> web.StreamResponse:
    monitor = request.app[MONITOR_KEY]
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await response.prepare(request)
    queue = monitor.subscribe()
    try:
        for event in monitor.snapshot()["events"][:20]:
            await response.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        while not request.transport.is_closing():
            event = await queue.get()
            await response.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        monitor.unsubscribe(queue)
    return response


async def ledger_snapshot(request: web.Request) -> web.Response:
    return web.json_response(request.app[KERNEL_KEY].ledger.snapshot(request.query.get("plugin", ""), request.query.get("account", ""), int(request.query.get("limit", "100"))))


async def models(request: web.Request) -> web.Response:
    return web.json_response({"object": "list", "data": request.app[KERNEL_KEY].list_models()})


async def status(request: web.Request) -> web.Response:
    return web.json_response({**asdict(request.app[KERNEL_KEY].status), "actionHistory": request.app[KERNEL_KEY].actions.status()})


async def plugins(request: web.Request) -> web.Response:
    return web.json_response(request.app[KERNEL_KEY].public({"plugins": request.app[KERNEL_KEY].list_plugins()}))


async def get_config(request: web.Request) -> web.Response:
    return web.json_response(request.app[KERNEL_KEY].config.public())


async def put_config(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        config = await request.app[CONFIG_KEY].update(body, request.app[KERNEL_KEY].configure)
        return web.json_response({"ok": True, "config": config.public()})
    except (ValueError, TypeError, RuntimeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def tasks(request: web.Request) -> web.Response:
    return web.json_response(request.app[KERNEL_KEY].scheduler.snapshot())


async def run_task(request: web.Request) -> web.Response:
    task_id = request.match_info["id"]
    for plugin in request.app[KERNEL_KEY]._snapshot.plugins.values():
        for task in plugin.spec.tasks:
            spec = TaskSpec(f"{plugin.name}:{task.get('id', task['action'])}", plugin.name, task["action"], float(task["intervalSeconds"]), task.get("accountId"), task.get("input",{}))
            if spec.id == task_id:
                success = await request.app[KERNEL_KEY].scheduler.run(spec, request.app[KERNEL_KEY].run_task)
                return web.json_response({"ok": success})
    return web.json_response({"error": "任务不存在或已卸载"}, status=404)


async def check_health(request: web.Request) -> web.Response:
    ran = await request.app[KERNEL_KEY].health_runner.run_once(request.app[KERNEL_KEY].check_health)
    if not ran:
        return web.json_response({"error": "巡检正在执行"}, status=409)
    return web.json_response(request.app[KERNEL_KEY].public({"ok": True, "plugins": request.app[KERNEL_KEY].list_plugins()}))


async def reload_kernel(request: web.Request) -> web.Response:
    try:
        status = await request.app[KERNEL_KEY].load()
        request.app[MONITOR_KEY].log("info", "kernel", "内核重载完成", generation=status.generation)
        return web.json_response({"ok": not status.faults, "generation": status.generation},status=409 if status.faults else 200)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def chat(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not isinstance(body.get("messages"), list):
            raise ValueError("model 与 messages 必填")
        kernel = request.app[KERNEL_KEY]
        chain = kernel.config.routes.get(body["model"], (body["model"],)) if kernel.config.fallback_enabled else (body["model"],)
        route = next((kernel.resolve(candidate) for candidate in chain if kernel.resolve(candidate)), None)
        if not route:
            return openai_error(404, "model_not_found", "模型不存在或所属插件未启用")
        if body.get("stream", False):
            selected: tuple[str, Any] | None = None
            iterator = None
            first_chunk: str | None = None
            last_error: Exception | None = None
            for candidate in chain:
                route = kernel.resolve(candidate)
                if not route:
                    continue
                plugin, model = route
                pending = kernel.stream(plugin, body, model)
                try:
                    first_chunk = await anext(pending)
                except StopAsyncIteration:
                    first_chunk = None
                except Exception as exc:
                    last_error = exc
                    await pending.aclose()
                    continue
                iterator = pending
                selected = (candidate, model)
                break
            if iterator is None:
                if last_error:
                    raise last_error
                return openai_error(503, "no_available_route", "没有可用插件")
            candidate, model = selected
            response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            if candidate != body["model"]:
                response.headers["X-Nachuan-Fallback"] = "true"
                response.headers["X-Nachuan-Model"] = candidate
            await response.prepare(request)
            try:
                if first_chunk is not None:
                    await response.write(("data: " + first_chunk + "\n\n").encode())
                async for chunk in iterator:
                    await response.write(("data: " + chunk + "\n\n").encode())
                await response.write(b"data: [DONE]\n\n")
            except (asyncio.CancelledError, ConnectionResetError):
                raise
            except Exception as exc:
                logging.getLogger("nachuan.http").warning("流式响应中断: %s", kernel.public(str(exc)))
                error = json.dumps({"error": {"message": kernel.public(str(exc)), "type": "stream_error", "code": getattr(exc, "kind", "upstream_error").value if hasattr(getattr(exc, "kind", None), "value") else "upstream_error"}}, ensure_ascii=False)
                await response.write(("data: " + error + "\n\n").encode())
                await response.write(b"data: [DONE]\n\n")
            finally:
                await iterator.aclose()
            await response.write_eof()
            return response
        last_error = None
        for candidate in chain:
            route = kernel.resolve(candidate)
            if not route:
                continue
            plugin, model = route
            try:
                result = await kernel.invoke(plugin, body, model)
                if candidate != body["model"]:
                    response = web.json_response(result)
                    response.headers["X-Nachuan-Fallback"] = "true"
                    response.headers["X-Nachuan-Model"] = candidate
                    return response
                return web.json_response(result)
            except (PluginFailure, PluginUnavailable) as exc:
                last_error = exc
                if isinstance(exc, PluginFailure) and exc.kind.value == "invalid_request":
                    raise
        if last_error:
            raise last_error
        return openai_error(503, "no_available_route", "没有可用插件")
    except web.HTTPException:
        raise
    except PluginFailure as exc:
        return openai_error(400 if exc.kind.value == "invalid_request" else 502, exc.kind.value, request.app[KERNEL_KEY].public(str(exc)))
    except PluginUnavailable as exc:
        status = 429 if "并发" in str(exc) else 503
        return openai_error(status, "plugin_unavailable", request.app[KERNEL_KEY].public(str(exc)))
    except (ValueError, json.JSONDecodeError) as exc:
        return openai_error(400, "invalid_request", str(exc))
    except Exception as exc:
        logging.getLogger("nachuan.http").warning("plugin invocation failed: %s", request.app[KERNEL_KEY].public(str(exc)))
        return openai_error(502, "plugin_error", request.app[KERNEL_KEY].public(str(exc)))


def openai_error(status_code: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"message": message, "type": "invalid_request_error", "code": code}}, status=status_code)


def load_settings(path: Path) -> dict[str, PluginSetting]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    plugins = raw.get("plugins", {})
    if isinstance(plugins, list):
        plugins = {item["id"]: item for item in plugins}
    return {name: PluginSetting(bool(item.get("enabled", True)), int(item.get("maxInFlight", 0))) for name, item in plugins.items()}


async def run(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    plugins_dir = Path(config.get("plugins_dir", "../plugins"))
    if not plugins_dir.is_absolute():
        plugins_dir = config_path.parent / plugins_dir
    kernel = Kernel(plugins_dir, runtime_dir=Path(args.runtime), node=args.node)
    await kernel.configure(HostConfig.from_dict(config))
    app = create_app(kernel, os.environ.get("NACHUAN_API_KEY", config.get("api_key", "")), config_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    logging.getLogger("nachuan").info("Python kernel listening on http://%s:%s", args.host, args.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="纳川 Python 宿主内核")
    parser.add_argument("--config", default=str(Path(__file__).parents[2] / "backend" / "config.local.json"))
    parser.add_argument("--runtime", default=str(Path(__file__).parents[2] / "plugin-runtime"))
    parser.add_argument("--node", default="node")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7865)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
