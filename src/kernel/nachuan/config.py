"""宿主热配置：单一代际、原子发布、失败回滚，不保存凭证。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
import asyncio
import json
import os

from .contracts import PluginSetting


@dataclass(frozen=True, slots=True)
class HostConfig:
    request_timeout_seconds: float = 120
    health_interval_seconds: float = 300
    max_request_bytes: int = 8 * 1024 * 1024
    fallback_enabled: bool = False
    routes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    plugins: dict[str, PluginSetting] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HostConfig":
        kernel = raw.get("kernel", {})
        plugins = raw.get("plugins", {})
        if isinstance(plugins, list):
            plugins = {item["id"]: item for item in plugins}
        settings = {name: PluginSetting(bool(item.get("enabled", True)), int(item.get("maxInFlight", 0))) for name, item in plugins.items()}
        timeout = float(kernel.get("requestTimeoutSeconds", 120))
        interval = float(kernel.get("healthIntervalSeconds", 300))
        max_bytes = int(kernel.get("maxRequestBytes", 8 * 1024 * 1024))
        if not 1 <= timeout <= 3600 or not 15 <= interval <= 86400 or not 1024 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError("内核配置超出允许范围")
        raw_routes = kernel.get("routes", {})
        if not isinstance(raw_routes, dict) or any(not isinstance(chain, list) or not chain or any(not isinstance(model, str) or not model for model in chain) for chain in raw_routes.values()):
            raise ValueError("routes 必须是模型别名到非空故障转移链的映射")
        routes = {str(alias): tuple(chain) for alias, chain in raw_routes.items()}
        return cls(timeout, interval, max_bytes, bool(kernel.get("fallbackEnabled", False)), routes, settings)

    def public(self) -> dict[str, Any]:
        return {"requestTimeoutSeconds": self.request_timeout_seconds, "healthIntervalSeconds": self.health_interval_seconds, "maxRequestBytes": self.max_request_bytes, "fallbackEnabled": self.fallback_enabled, "routes": {alias: list(chain) for alias, chain in self.routes.items()}, "plugins": {name: {"enabled": setting.enabled, "maxInFlight": setting.max_in_flight} for name, setting in self.plugins.items()}}


class ConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._raw: dict[str, Any] = {}

    def load(self) -> HostConfig:
        self._raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        return HostConfig.from_dict(self._raw)

    async def update(self, kernel_config: dict[str, Any], apply: Callable[[HostConfig], Awaitable[None]]) -> HostConfig:
        async with self._lock:
            if not self._raw and self.path.exists():
                self._raw = json.loads(self.path.read_text(encoding="utf-8"))
            old_raw = self._raw
            candidate_raw = dict(old_raw)
            candidate_raw["kernel"] = {key: value for key, value in kernel_config.items() if key != "plugins"}
            if "plugins" in kernel_config:
                candidate_raw["plugins"] = kernel_config["plugins"]
            candidate = HostConfig.from_dict(candidate_raw)
            previous_raw = self.path.read_bytes() if self.path.exists() else None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(candidate_raw, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            try:
                await apply(candidate)
            except Exception:
                if previous_raw is None:
                    self.path.unlink(missing_ok=True)
                else:
                    rollback = self.path.with_suffix(".rollback")
                    rollback.write_bytes(previous_raw)
                    rollback.chmod(0o600)
                    os.replace(rollback, self.path)
                self._raw = old_raw
                raise
            self._raw = candidate_raw
            return candidate
