"""通用周期任务调度：声明式 interval、单实例、并发上限与有界历史。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
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
    account_id: str | None = None
    input: dict[str, Any] = field(default_factory=dict)


class Scheduler:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running: set[str] = set()
        self._executions: dict[str, asyncio.Task] = {}
        self._specs: dict[str, TaskSpec] = {}
        self._next: dict[str,float] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=100)

    async def register(self, spec: TaskSpec, callback: Callable[[TaskSpec], Awaitable[dict[str, Any]]]) -> None:
        if spec.interval_seconds < 15:
            raise ValueError("任务间隔不得小于 15 秒")
        await self.unregister(spec.id)
        self._specs[spec.id]=spec
        self._next[spec.id]=time.time()+spec.interval_seconds
        self._tasks[spec.id] = asyncio.create_task(self._loop(spec, callback))

    async def _loop(self, spec: TaskSpec, callback: Callable[[TaskSpec], Awaitable[dict[str, Any]]]) -> None:
        while True:
            await asyncio.sleep(spec.interval_seconds)
            await self.run(spec, callback)
            self._next[spec.id]=time.time()+spec.interval_seconds

    async def run(self, spec: TaskSpec, callback: Callable[[TaskSpec], Awaitable[dict[str, Any]]]) -> bool:
        if spec.id in self._running:
            return False
        self._running.add(spec.id)
        self._executions[spec.id]=asyncio.current_task()
        started = time.time()
        try:
            result = await callback(spec)
            self._history.append({"id": uuid.uuid4().hex[:12], "task_id": spec.id, "started_at": started, "duration_ms": round((time.time() - started) * 1000), "success": True, "result": result})
            return True
        except asyncio.CancelledError:
            self._history.append({'id':uuid.uuid4().hex[:12],'task_id':spec.id,'started_at':started,'success':False,'error':'cancelled'})
            raise
        except Exception as exc:
            self._history.append({"id": uuid.uuid4().hex[:12], "task_id": spec.id, "started_at": started, "duration_ms": round((time.time() - started) * 1000), "success": False, "error": str(exc)[:500]})
            return False
        finally:
            self._running.discard(spec.id)
            self._executions.pop(spec.id,None)

    async def unregister(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        self._specs.pop(task_id,None)
        self._next.pop(task_id,None)
        execution=self._executions.get(task_id)
        pending={t for t in (task,execution) if t and t is not asyncio.current_task()}
        for running in pending: running.cancel()
        await asyncio.gather(*pending,return_exceptions=True)

    def snapshot(self) -> dict[str, Any]:
        return {"tasks": [{"id": task_id, "running": task_id in self._running, 'nextRunAt':self._next.get(task_id),
                           'lastRunAt':next((r['started_at'] for r in reversed(self._history) if r['task_id']==task_id),None),
                           'lastResult':next((r['success'] for r in reversed(self._history) if r['task_id']==task_id),None)} for task_id in sorted(self._tasks)], "history": list(reversed(self._history))}

    async def close(self) -> None:
        for task_id in set(self._tasks)|set(self._executions):
            await self.unregister(task_id)
