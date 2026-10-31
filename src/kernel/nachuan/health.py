"""凭证生命周期与低频健康巡检；平台探测在 TypeScript 业务层执行。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
import asyncio
import logging
import time

log = logging.getLogger("nachuan.health")

@dataclass(slots=True)
class HealthEvidence:
    configured: bool = False
    reachable: bool = False
    authenticated: bool = False
    models_ready: bool = False
    account_ready: bool = False
    last_checked: float = 0
    last_success: float = 0
    error: str = ""

    @property
    def ready(self) -> bool:
        return self.configured and self.reachable and self.authenticated and self.models_ready and self.account_ready

    def snapshot(self) -> dict[str, Any]:
        return {**asdict(self), "ready": self.ready, "last_checked_iso": _iso(self.last_checked), "last_success_iso": _iso(self.last_success)}


def _iso(timestamp: float) -> str | None:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None


def expires_soon(credential: dict[str, Any], now: float, within_seconds: float = 600) -> bool:
    expires = credential.get("expiresAt", credential.get("expires_at", 0))
    try:
        return bool(expires) and float(expires) <= now + within_seconds
    except (TypeError, ValueError):
        return False


class HealthRunner:
    """有界、独立调度的巡检任务；每个插件错误只影响自己。"""
    def __init__(self, interval_seconds: float = 300):
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._wake = asyncio.Event()
        self._manual_lock = asyncio.Lock()

    def start(self, callback: Callable[[], Awaitable[None]]) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(callback))

    async def _loop(self, callback: Callable[[], Awaitable[None]]) -> None:
        while True:
            if not self._running:
                self._running = True
                try:
                    await callback()
                except Exception:
                    log.exception("健康巡检异常，下一轮继续")
                finally:
                    self._running = False
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.interval_seconds)
            except TimeoutError:
                pass

    def set_interval(self, seconds: float) -> None:
        if not 15 <= seconds <= 86400:
            raise ValueError("巡检间隔须在 15–86400 秒之间")
        self.interval_seconds = seconds
        self._wake.set()

    async def run_once(self, callback: Callable[[], Awaitable[None]]) -> bool:
        async with self._manual_lock:
            if self._running:
                return False
            self._running = True
        try:
            await callback()
            return True
        finally:
            self._running = False

    async def close(self) -> None:
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, 2)
            except asyncio.CancelledError:
                pass
