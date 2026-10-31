"""纳川 Python 宿主：生命周期、原子路由、插件隔离与并发水库。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, AsyncIterator, Callable, Mapping
import json
import logging
import time

from .contracts import ContractError, Lifecycle, Model, PluginSetting, PluginSpec, PluginUnavailable, validate_spec
from .accounts import AccountPool, FailureKind, Lease
from .errors import as_failure
from .monitoring import Monitor
from .health import HealthEvidence, HealthRunner, expires_soon
from .config import HostConfig
from .tasks import Scheduler, TaskSpec
from .streaming import StreamNormalizer
from .transport import PluginTransport, ProcessTransport

log = logging.getLogger("nachuan.kernel")
TransportFactory = Callable[[list[str], str], PluginTransport]


@dataclass(slots=True)
class PluginInstance:
    spec: PluginSpec
    directory: Path
    setting: PluginSetting
    generation: int
    status: Lifecycle = Lifecycle.STARTING
    fault: str | None = None
    transport: PluginTransport | None = None
    semaphore: asyncio.Semaphore | None = None
    accepts_requests: bool = True
    watchdog: asyncio.Task[None] | None = None
    in_flight: int = 0
    retired: bool = False
    account_pool: AccountPool | None = None
    monitor: Monitor | None = None
    last_account: str = ""
    health: HealthEvidence = field(default_factory=HealthEvidence)

    def __post_init__(self) -> None:
        if self.setting.max_in_flight > 0:
            self.semaphore = asyncio.Semaphore(self.setting.max_in_flight)

    @property
    def name(self) -> str:
        return self.spec.name

    async def start(self, factory: TransportFactory, runtime: Path, node: str) -> None:
        if not self.setting.enabled:
            self.status = Lifecycle.DISABLED
            return
        try:
            self.account_pool = AccountPool(self.directory / "credentials")
            self.account_pool.load()
            self.account_pool.start()
            if not self.account_pool.snapshot():
                self.status = Lifecycle.CREDENTIAL_MISSING
                self.fault = "未配置账号凭证"
                await self.close()
                raise ContractError(self.fault)
            self.transport = factory([node, str(runtime / "dist" / "host.js"), str(self.directory / self.spec.entry)], str(self.directory))
            await self.transport.start()
            result = await self.transport.call("plugin.init", {"spec": {"name": self.name, "version": self.spec.version}, "config": self.spec.settings}, 15)
            if not isinstance(result, dict) or result.get("protocol") != 1:
                raise ContractError("插件握手协议版本不兼容")
            models = result.get("models")
            if not isinstance(models, list) or {item.get("id") for item in models if isinstance(item, dict)} != {model.id for model in self.spec.models}:
                raise ContractError("插件运行期模型与 spec.json 不一致")
            self.status = Lifecycle.RUNNING
            if self.monitor:
                self.monitor.log("info", self.name, "插件已就绪")
            if self.transport.process:
                self.watchdog = asyncio.create_task(self._watch(self.transport.process))
        except Exception as exc:
            if self.status != Lifecycle.CREDENTIAL_MISSING:
                self.status = Lifecycle.FAULT
            self.fault = str(exc)
            await self.close()
            raise

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        if self.accepts_requests:
            self.status = Lifecycle.FAULT
            self.fault = f"插件进程意外退出，退出码 {code}"
            self.accepts_requests = False
            log.error("插件 %s 已隔离: %s", self.name, self.fault)
            if self.monitor:
                self.monitor.log("error", self.name, self.fault)

    async def close(self) -> None:
        self.accepts_requests = False
        if self.watchdog:
            self.watchdog.cancel()
        if self.transport:
            await self.transport.close()
        if self.account_pool:
            await self.account_pool.close()
        if self.status != Lifecycle.CREDENTIAL_MISSING:
            self.status = Lifecycle.STOPPED


@dataclass(frozen=True, slots=True)
class Snapshot:
    plugins: Mapping[str, PluginInstance]
    models: Mapping[str, tuple[PluginInstance, Model]]


@dataclass(slots=True)
class KernelStatus:
    generation: int = 0
    loaded: int = 0
    running: int = 0
    disabled: int = 0
    faults: int = 0
    ambiguous_models: list[str] | None = None
    last_error: str | None = None


class Kernel:
    """宿主唯一外部接口；调用方不感知插件文件、进程或路由索引。"""

    def __init__(self, plugins_dir: str | Path, *, runtime_dir: str | Path | None = None, node: str = "node", transport_factory: TransportFactory = ProcessTransport):
        self.plugins_dir = Path(plugins_dir).resolve()
        self.runtime_dir = (Path(runtime_dir) if runtime_dir else Path(__file__).parents[2] / "plugin-runtime").resolve()
        self.node = node
        self.transport_factory = transport_factory
        from .ledger import Ledger
        self.ledger = Ledger(self.plugins_dir.parent / "data" / "usage-ledger.json")
        self.ledger.load()
        self.ledger.start()
        self.monitor = Monitor(ledger=self.ledger)
        self.health_runner = HealthRunner()
        self.config = HostConfig()
        self.scheduler = Scheduler()
        self._closed = False
        self.health_runner.start(self.check_health)
        self._snapshot = Snapshot(MappingProxyType({}), MappingProxyType({}))
        self._retiring: set[asyncio.Task[None]] = set()
        self._faults: dict[str, str] = {}
        self._settings: dict[str, PluginSetting] = {}
        self._reload_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self.status = KernelStatus()

    def _read(self, generation: int) -> list[PluginInstance]:
        if not self.plugins_dir.is_dir():
            raise ContractError(f"插件目录不存在: {self.plugins_dir}")
        instances: list[PluginInstance] = []
        for directory in sorted(self.plugins_dir.iterdir()):
            path = directory / "spec.json"
            if not directory.is_dir() or not path.exists():
                continue
            try:
                spec = validate_spec(json.loads(path.read_text(encoding="utf-8")), str(directory))
                setting = self._settings.get(spec.name, PluginSetting())
                instances.append(PluginInstance(spec, directory, setting, generation))
            except (ContractError, ValueError, OSError) as exc:
                spec = PluginSpec(directory.name, "unknown", "index.ts", ())
                instances.append(PluginInstance(spec, directory, PluginSetting(False), generation, Lifecycle.FAULT, str(exc)))
        return instances

    async def load(self, settings: Mapping[str, PluginSetting] | None = None) -> KernelStatus:
        async with self._reload_lock:
            previous_settings = self._settings
            if settings is not None:
                self._settings = dict(settings)
            generation = self.status.generation + 1
            candidates = self._read(generation)
            try:
                self._validate_candidates(candidates)
            except Exception as exc:
                self._settings = previous_settings
                self.status.last_error = str(exc)
                raise
            next_plugins: dict[str, PluginInstance] = {}
            faults = 0
            for candidate in candidates:
                previous = self._snapshot.plugins.get(candidate.name)
                if candidate.status == Lifecycle.FAULT:
                    faults += 1
                    log.warning("插件 %s 契约故障: %s", candidate.name, candidate.fault)
                    self.monitor.log("error", candidate.name, candidate.fault or "插件契约故障")
                    next_plugins[candidate.name] = previous or candidate
                    continue
                try:
                    await candidate.start(self.transport_factory, self.runtime_dir, self.node)
                    next_plugins[candidate.name] = candidate
                except Exception as exc:
                    if candidate.status != Lifecycle.CREDENTIAL_MISSING:
                        faults += 1
                    candidate.fault = str(exc)
                    log.warning("插件 %s 启动失败: %s", candidate.name, candidate.fault)
                    self.monitor.log("warning" if candidate.status == Lifecycle.CREDENTIAL_MISSING else "error", candidate.name, candidate.fault)
                    if previous:
                        next_plugins[candidate.name] = previous
                    elif candidate.status == Lifecycle.CREDENTIAL_MISSING:
                        next_plugins[candidate.name] = candidate
            self._faults = {candidate.name: candidate.fault for candidate in candidates if candidate.status == Lifecycle.FAULT and candidate.fault}
            self._faults.update({candidate.name: candidate.fault for candidate in candidates if candidate.status == Lifecycle.CREDENTIAL_MISSING and candidate.fault})
            routes: dict[str, tuple[PluginInstance, Model]] = {}
            ambiguous: set[str] = set()
            for plugin in next_plugins.values():
                if plugin.status != Lifecycle.RUNNING:
                    continue
                for model in plugin.spec.models:
                    if model.id in routes:
                        ambiguous.add(model.id)
                    else:
                        routes[model.id] = (plugin, model)
                    routes[f"{plugin.spec.prefix}:{model.id}"] = (plugin, model)
            for model_id in ambiguous:
                routes.pop(model_id, None)
            old = self._snapshot.plugins
            self._snapshot = Snapshot(MappingProxyType(next_plugins), MappingProxyType(routes))
            for name, plugin in old.items():
                if next_plugins.get(name) is not plugin:
                    self._retire(plugin)
            self.status.loaded = len(candidates)
            active_tasks = {f"{plugin.name}:{task['action']}" for plugin in next_plugins.values() if plugin.status == Lifecycle.RUNNING for task in plugin.spec.tasks}
            for task_id in list(self.scheduler._tasks):
                if task_id not in active_tasks:
                    await self.scheduler.unregister(task_id)
            for plugin in next_plugins.values():
                if plugin.status == Lifecycle.RUNNING:
                    for task in plugin.spec.tasks:
                        spec = TaskSpec(f"{plugin.name}:{task['action']}", plugin.name, task["action"], float(task["intervalSeconds"]))
                        await self.scheduler.register(spec, self.run_task)
            self.status.running = sum(plugin.status == Lifecycle.RUNNING for plugin in next_plugins.values())
            self.status.disabled = sum(plugin.status == Lifecycle.DISABLED for plugin in candidates)
            self.status.faults = faults
            self.status.ambiguous_models = sorted(ambiguous)
            self.status.generation = generation
            self.status.last_error = None
            log.info("kernel generation=%s running=%s disabled=%s faults=%s", self.status.generation, self.status.running, self.status.disabled, self.status.faults)
            return self.status

    async def check_health(self) -> None:
        for plugin in list(self._snapshot.plugins.values()):
            if plugin.status != Lifecycle.RUNNING or not plugin.transport or not plugin.account_pool:
                continue
            try:
                await self.refresh_credentials(plugin)
                accounts = plugin.account_pool.snapshot()
                if not accounts:
                    plugin.health = HealthEvidence(last_checked=time.time(), error="未配置账号凭证")
                    continue
                account = next((item for item in plugin.account_pool._accounts.values() if item.enabled), None)
                if not account:
                    plugin.health = HealthEvidence(configured=True, last_checked=time.time(), error="没有启用账号")
                    continue
                evidence = await plugin.transport.call("credential.health", {"credential": {"id": account.id, "secret": account.credential()}}, 15)
                plugin.health = HealthEvidence(True, bool(evidence.get("reachable")), bool(evidence.get("authenticated")), bool(evidence.get("modelsReady")), bool(evidence.get("accountReady")), time.time(), time.time())
                self.monitor.log("info", plugin.name, "健康巡检通过" if plugin.health.ready else "健康巡检未就绪", ready=plugin.health.ready)
            except Exception as exc:
                plugin.health.last_checked = time.time()
                plugin.health.error = str(exc)
                self.monitor.log("warning", plugin.name, f"健康巡检失败: {exc}")

    async def refresh_credentials(self, plugin: PluginInstance) -> None:
        if not plugin.account_pool or not plugin.transport:
            return
        for account in plugin.account_pool._accounts.values():
            credential = account.credential()
            needs_refresh = expires_soon(credential, time.time()) or account.state == AccountState.REAUTH_REQUIRED
            if not needs_refresh or not account.refresh_supported:
                continue
            generation = account.credential_generation
            refreshed = await plugin.transport.call("credential.refresh", {"credential": {"id": account.id, "secret": credential}}, 30)
            if not isinstance(refreshed, dict):
                raise RuntimeError("刷新结果必须为凭证对象")
            await plugin.account_pool.rotate(account.id, refreshed, expected_generation=generation, recovered=True)
            self.monitor.log("info", plugin.name, "凭证已刷新", account=account.id)

    async def configure(self, config: HostConfig) -> None:
        async with self._config_lock:
            previous = self.config
            try:
                await self.load(config.plugins)
                self.health_runner.set_interval(config.health_interval_seconds)
                self.config = config
                self.monitor.log("info", "kernel", "热配置已发布", fallback_enabled=config.fallback_enabled)
            except Exception:
                self.config = previous
                raise

    @staticmethod
    def _validate_candidates(candidates: list[PluginInstance]) -> None:
        names: set[str] = set()
        for plugin in candidates:
            if plugin.name in names:
                raise ContractError(f"插件名称重复: {plugin.name}")
            names.add(plugin.name)

    def list_models(self) -> list[dict[str, Any]]:
        result = [{"id": f"{plugin.spec.prefix}:{model.id}", "object": "model", "owned_by": plugin.name, "max_output_tokens": model.max_tokens} for plugin, model in self._snapshot.models.values() if plugin.status == Lifecycle.RUNNING]
        return sorted({item["id"]: item for item in result}.values(), key=lambda item: item["id"])

    def list_plugins(self) -> list[dict[str, Any]]:
        return [{"name": plugin.name, "version": plugin.spec.version, "status": plugin.status.value, "enabled": plugin.setting.enabled, "fault": self._faults.get(plugin.name, plugin.fault), "generation": plugin.generation, "models": len(plugin.spec.models), "health": plugin.health.snapshot(), "accounts": plugin.account_pool.snapshot() if plugin.account_pool else []} for _, plugin in sorted(self._snapshot.plugins.items())]

    def resolve(self, model_id: str) -> tuple[PluginInstance, Model] | None:
        route = self._snapshot.models.get(model_id)
        if route and (route[0].status != Lifecycle.RUNNING or not route[0].accepts_requests):
            return None
        return route

    def pool_for(self, plugin: PluginInstance) -> AccountPool:
        if plugin.account_pool is None:
            raise PluginUnavailable("插件尚未配置账号池")
        return plugin.account_pool

    @staticmethod
    def _params(request: dict[str, Any], model: Model, lease: Lease | None) -> dict[str, Any]:
        credential = {"id": "default", "secret": {}} if lease is None else {"id": lease.account.id, "secret": lease.account.credential()}
        return {"request": request, "model": model.upstream_id, "credential": credential}

    async def _permit(self, plugin: PluginInstance) -> None:
        if not plugin.transport or (plugin.status != Lifecycle.RUNNING and not plugin.retired) or not plugin.transport or plugin.transport.closed:
            raise PluginUnavailable("插件当前不可接收请求")
        if not plugin.accepts_requests and not plugin.retired:
            raise PluginUnavailable("插件当前不可接收请求")
        if plugin.semaphore:
            if plugin.semaphore.locked():
                raise PluginUnavailable("插件并发已满")
            await plugin.semaphore.acquire()
        plugin.in_flight += 1

    def _retire(self, plugin: PluginInstance) -> None:
        plugin.retired = True
        plugin.accepts_requests = False
        async def close_when_drained() -> None:
            deadline = time.monotonic() + self.config.request_timeout_seconds
            try:
                while plugin.in_flight and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
            finally:
                await plugin.close()
        task = asyncio.create_task(close_when_drained())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    def _release(self, plugin: PluginInstance) -> None:
        plugin.in_flight = max(0, plugin.in_flight - 1)
        if plugin.semaphore:
            plugin.semaphore.release()

    def _accounts(self, plugin: PluginInstance) -> list[AccountPool | None]:
        return [plugin.account_pool] if plugin.account_pool and plugin.account_pool.snapshot() else [None]

    async def invoke(self, plugin: PluginInstance, request: dict[str, Any], model: Model) -> dict[str, Any]:
        started = time.monotonic()
        request_id = self.monitor.start(plugin.name, model.id, False)
        result: dict[str, Any] | None = None
        error = ""
        try:
            result = await self._invoke(plugin, request, model)
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            self.monitor.finish(plugin.name, request_id, started, model=model.id, result=result, error=error, account=plugin.last_account)

    async def _invoke(self, plugin: PluginInstance, request: dict[str, Any], model: Model) -> dict[str, Any]:
        await self._permit(plugin)
        last_error: Exception | None = None
        try:
            for pool in self._accounts(plugin):
                attempts = len(pool.snapshot()) if pool else 1
                for _ in range(attempts):
                    lease: Lease | None = None
                    try:
                        if pool:
                            lease = await pool.acquire()
                        plugin.last_account = lease.account.id if lease else ""
                        result = await plugin.transport.call("chat.completion", self._params(request, model, lease), self.config.request_timeout_seconds)
                        if lease:
                            await lease.release(None)
                        return result
                    except asyncio.CancelledError:
                        if lease:
                            await lease.release(None)
                        raise
                    except Exception as exc:
                        failure = as_failure(exc)
                        if lease:
                            await lease.release(failure.account_failure())
                        last_error = failure
                        if failure.kind == FailureKind.INVALID_REQUEST or not pool:
                            raise failure from exc
                    if pool is None:
                        break
            raise PluginUnavailable(str(last_error or "没有可用账号"))
        finally:
            self._release(plugin)

    async def stream(self, plugin: PluginInstance, request: dict[str, Any], model: Model) -> AsyncIterator[str]:
        started = time.monotonic()
        request_id = self.monitor.start(plugin.name, model.id, True)
        error = ""
        normalizer = StreamNormalizer(model.id, request_id=f"chatcmpl-{request_id}")
        first_token_at: float | None = None
        source = self._stream(plugin, request, model)
        try:
            async for data in source:
                if data.strip() == "[DONE]":
                    break
                for chunk in normalizer.data(data):
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    yield json.dumps(chunk, ensure_ascii=False)
            for chunk in normalizer.finish():
                yield json.dumps(chunk, ensure_ascii=False)
        except (asyncio.CancelledError, GeneratorExit):
            error = "请求已取消"
            raise
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            await source.aclose()
            self.monitor.finish(plugin.name, request_id, started, model=model.id, result={"usage": {"prompt_tokens": normalizer.usage.prompt_tokens, "completion_tokens": normalizer.usage.completion_tokens, "total_tokens": normalizer.usage.total_tokens}}, error=error, first_token_at=first_token_at, account=plugin.last_account)

    async def _stream(self, plugin: PluginInstance, request: dict[str, Any], model: Model) -> AsyncIterator[str]:
        await self._permit(plugin)
        try:
            for pool in self._accounts(plugin):
                attempts = len(pool.snapshot()) if pool else 1
                for _ in range(attempts):
                    lease: Lease | None = None
                    emitted = False
                    try:
                        if pool:
                            lease = await pool.acquire()
                        plugin.last_account = lease.account.id if lease else ""
                        async for chunk in plugin.transport.stream("chat.stream", self._params(request, model, lease), self.config.request_timeout_seconds):
                            emitted = True
                            yield chunk
                        if lease:
                            await lease.release(None)
                        return
                    except (asyncio.CancelledError, GeneratorExit):
                        if lease:
                            await lease.release(None)
                        raise
                    except Exception as exc:
                        failure = as_failure(exc)
                        if lease:
                            await lease.release(failure.account_failure())
                        if emitted or failure.kind == FailureKind.INVALID_REQUEST or not pool:
                            raise failure from exc
            raise PluginUnavailable("没有可用账号")
        finally:
            self._release(plugin)

    async def run_task(self, spec: TaskSpec) -> dict[str, Any]:
        plugin = self._snapshot.plugins.get(spec.plugin)
        if not plugin or plugin.status != Lifecycle.RUNNING or not plugin.transport:
            raise RuntimeError("插件未运行")
        result = await plugin.transport.call("task.execute", {"action": spec.action}, self.config.request_timeout_seconds)
        self.monitor.log("info", spec.plugin, "周期任务完成", task_id=spec.id)
        return result if isinstance(result, dict) else {"result": result}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.scheduler.close()
        for plugin in {id(plugin): plugin for plugin in self._snapshot.plugins.values()}.values():
            await plugin.close()
        await self.ledger.close()
        await self.health_runner.close()
        for task in list(self._retiring):
            task.cancel()
        if self._retiring:
            await asyncio.gather(*self._retiring, return_exceptions=True)
        self._snapshot = Snapshot(MappingProxyType({}), MappingProxyType({}))
