"""通用周期任务调度：声明式 interval、单实例、并发上限与有界历史。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import asyncio
import time
import uuid


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    plugin: str
    action: str
    interval_seconds: float


class Scheduler:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running: set[str] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=100)

    async def register(self, spec: TaskSpec, callback: Callable[[TaskSpec], Awaitable[dict[str, Any]]]) -> None:
        if spec.interval_seconds < 15:
            raise ValueError("任务间隔不得小于 15 秒")
        await self.unregister(spec.id)
        self._tasks[spec.id] = asyncio.create_task(self._loop(spec, callback))

    async def _loop(self, spec: TaskSpec, callback: Callable[[TaskSpec], Awaitable[dict[str, Any]]]) -> None:
        while True:
            await asyncio.sleep(spec.interval_seconds)
            await self.run(spec, callback)

    async def run(self, spec: TaskSpec, callback: Callable[[TaskSpec], Awaitable[dict[str, Any]]]) -> bool:
        if spec.id in self._running:
            return False
        self._running.add(spec.id)
        started = time.time()
        try:
            result = await callback(spec)
            self._history.append({"id": uuid.uuid4().hex[:12], "task_id": spec.id, "started_at": started, "duration_ms": round((time.time() - started) * 1000), "success": True, "result": result})
            return True
        except Exception as exc:
            self._history.append({"id": uuid.uuid4().hex[:12], "task_id": spec.id, "started_at": started, "duration_ms": round((time.time() - started) * 1000), "success": False, "error": str(exc)[:500]})
            return False
        finally:
            self._running.discard(spec.id)

    async def unregister(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def snapshot(self) -> dict[str, Any]:
        return {"tasks": [{"id": task_id, "running": task_id in self._running} for task_id in sorted(self._tasks)], "history": list(reversed(self._history))}

    async def close(self) -> None:
        for task_id in list(self._tasks):
            await self.unregister(task_id)
