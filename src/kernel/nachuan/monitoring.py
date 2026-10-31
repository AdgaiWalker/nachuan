"""监控内核：有界指标、隔离日志与只读快照，不含凭证和请求正文。"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from typing import Any
from .ledger import Ledger, UsageRecord
import asyncio
import re
import time
import uuid

_SECRET = re.compile(r'(?i)(bearer\s+|sk-)[A-Za-z0-9._~+/=-]+')


def sanitize(value: str) -> str:
    return _SECRET.sub(lambda match: match.group(1) + "••••", value.replace("\n", " "))[:500]


@dataclass(slots=True)
class PluginMetrics:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    in_flight: int = 0
    tokens: int = 0
    latency_ms: float = 0
    last_latency_ms: float = 0
    ttft_ms: float = 0
    total_ttft_ms: float = 0
    ttft_samples: int = 0

    @property
    def average_ttft_ms(self) -> int:
        return round(self.total_ttft_ms / self.ttft_samples) if self.ttft_samples else 0

    @property
    def success_rate(self) -> float:
        return round(self.successes / self.requests * 100, 1) if self.requests else 100.0

    @property
    def average_latency_ms(self) -> int:
        return round(self.latency_ms / self.requests) if self.requests else 0


class Monitor:
    def __init__(self, event_limit: int = 300, bucket_count: int = 24, ledger: Ledger | None = None):
        self.ledger = ledger
        self.started_at = time.time()
        self._metrics: dict[str, PluginMetrics] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=event_limit)
        self._buckets: deque[dict[str, Any]] = deque(maxlen=bucket_count)
        self._last_bucket: int | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def plugin(self, name: str) -> PluginMetrics:
        return self._metrics.setdefault(name, PluginMetrics())

    def start(self, plugin: str, model: str, streaming: bool) -> str:
        request_id = uuid.uuid4().hex[:12]
        self.plugin(plugin).in_flight += 1
        self.log("info", plugin, "请求开始", request_id=request_id, model=model, streaming=streaming)
        return request_id

    def finish(self, plugin: str, request_id: str, started: float, *, model: str, result: dict[str, Any] | None = None, error: str = "", account: str = "", first_token_at: float | None = None) -> None:
        metrics = self.plugin(plugin)
        metrics.in_flight = max(0, metrics.in_flight - 1)
        metrics.requests += 1
        metrics.last_latency_ms = (time.monotonic() - started) * 1000
        metrics.latency_ms += metrics.last_latency_ms
        if first_token_at is not None:
            metrics.ttft_ms = (first_token_at - started) * 1000
            metrics.total_ttft_ms += metrics.ttft_ms
            metrics.ttft_samples += 1
        usage = (result or {}).get("usage", {})
        tokens = int(usage.get("total_tokens", 0)) if isinstance(usage, dict) else 0
        prompt_tokens = int(usage.get("prompt_tokens", 0)) if isinstance(usage, dict) else 0
        completion_tokens = int(usage.get("completion_tokens", 0)) if isinstance(usage, dict) else 0
        metrics.tokens += max(tokens, 0)
        if self.ledger:
            import asyncio
            vendor = usage.get("vendor", {}) if isinstance(usage, dict) else {}
            task = self.ledger.record(UsageRecord(request_id, time.time(), plugin, model, account, bool(first_token_at is not None), not error, round(metrics.last_latency_ms), prompt_tokens, completion_tokens, tokens, "upstream" if usage else "unreported", vendor.get("cost"), str(vendor.get("unit", "")), vendor.get("remaining"), vendor.get("resetAt")))
            try:
                asyncio.get_running_loop().create_task(task)
            except RuntimeError:
                asyncio.run(task)
        if error:
            metrics.failures += 1
            self.log("error", plugin, sanitize(error), request_id=request_id, model=model, account=account, duration_ms=round(metrics.last_latency_ms))
        else:
            metrics.successes += 1
            self.log("info", plugin, "请求完成", request_id=request_id, model=model, account=account, duration_ms=round(metrics.last_latency_ms), tokens=tokens)
        self._record_bucket(metrics.last_latency_ms, not error)

    def _record_bucket(self, latency_ms: float, success: bool) -> None:
        bucket = int(time.time() // 3600) * 3600
        if self._last_bucket != bucket or not self._buckets:
            self._last_bucket = bucket
            self._buckets.append({"timestamp": bucket, "requests": 0, "failures": 0, "latency_ms": 0})
        current = self._buckets[-1]
        current["requests"] += 1
        current["failures"] += int(not success)
        current["latency_ms"] = round((current["latency_ms"] * (current["requests"] - 1) + latency_ms) / current["requests"])

    def log(self, level: str, plugin: str, message: str, **fields: Any) -> None:
        safe_fields = {key: sanitize(value) if isinstance(value, str) else value for key, value in fields.items() if key != "credential"}
        event = {"id": uuid.uuid4().hex, "timestamp": time.time(), "level": level, "plugin": plugin, "message": sanitize(message), **safe_fields}
        self._events.append(event)
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def snapshot(self) -> dict[str, Any]:
        metrics = {name: {**asdict(metric), "success_rate": metric.success_rate, "average_latency_ms": metric.average_latency_ms} for name, metric in self._metrics.items()}
        totals = Counter()
        for metric in self._metrics.values():
            for key in ("requests", "successes", "failures", "in_flight", "tokens"):
                totals[key] += getattr(metric, key)
        completed = totals["successes"] + totals["failures"]
        return {
            "started_at": self.started_at,
            "uptime_seconds": round(time.time() - self.started_at),
            "totals": {**totals, "success_rate": round(totals["successes"] / completed * 100, 1) if completed else 100.0},
            "plugins": metrics,
            "timeline": list(self._buckets),
            "events": list(reversed(self._events)),
        }
