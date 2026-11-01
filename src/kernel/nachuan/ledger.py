"""双轨用量账本：物理观测与上游结算分离，request_id 幂等。"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import time
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class UsageRecord:
    request_id: str
    timestamp: float
    plugin: str
    model: str
    account: str
    streaming: bool
    success: bool
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    usage_source: str
    vendor_cost: float | None = None
    vendor_unit: str = ""
    vendor_remaining: float | None = None
    quota_reset_at: float | None = None
    vendor_dimension: str = ""


class Ledger:
    def __init__(self, path: str | Path, limit: int = 20_000):
        self.path = Path(path)
        self.limit = limit
        self._records: dict[str, UsageRecord] = {}
        self._lock = asyncio.Lock()
        self._loaded = False
        self._queue: asyncio.Queue[UsageRecord | None] = asyncio.Queue(maxsize=2048)
        self._writer: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self._closed = False

    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    async def _write_loop(self) -> None:
        while (record := await self._queue.get()) is not None:
            async with self._lock:
                self._records[record.request_id] = record
                if len(self._records) > self.limit:
                    for request_id in list(self._records)[:len(self._records) - self.limit]:
                        self._records.pop(request_id, None)
                try:
                    await self._save()
                    self.last_error = None
                except OSError as exc:
                    self.last_error = str(exc)
            self._queue.task_done()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer:
            await asyncio.wait_for(self._queue.join(), 5)
            await self._queue.put(None)
            await asyncio.wait_for(self._writer, 5)

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("records", [])[-self.limit:]:
                record = UsageRecord(**item)
                self._records[record.request_id] = record
        except (OSError, ValueError, TypeError):
            # 账本损坏不伪造历史；新结算仍从当前进程开始并保留诊断。
            self._records = {}

    async def record(self, record: UsageRecord) -> bool:
        self.load()
        async with self._lock:
            if record.request_id in self._records:
                return False
            if self._writer:
                self._queue.put_nowait(record)
            else:
                self._records[record.request_id] = record
                if len(self._records) > self.limit:
                    for request_id in list(self._records)[:len(self._records) - self.limit]:
                        self._records.pop(request_id, None)
                await self._save()
        return True

    async def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"records": [asdict(record) for record in self._records.values()]}, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.path.chmod(0o600)

    def snapshot(self, plugin: str = "", account: str = "", limit: int = 100) -> dict[str, Any]:
        self.load()
        records = [record for record in self._records.values() if (not plugin or record.plugin == plugin) and (not account or record.account == account)]
        totals = Counter()
        settlements = {}
        unclassified = 0
        for record in records:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "duration_ms"):
                totals[key] += getattr(record, key)
            totals["requests"] += 1
            totals["successes"] += int(record.success)
            totals["failures"] += int(not record.success)
            if record.vendor_cost is not None:
                try: amount=Decimal(str(record.vendor_cost))
                except InvalidOperation: amount=Decimal('NaN')
                if not record.vendor_unit or not amount.is_finite() or amount<0:
                    unclassified+=1
                else:
                    key=(record.plugin,record.vendor_unit,record.vendor_dimension)
                    settlements[key]=settlements.get(key,Decimal(0))+amount
        by_plugin: dict[str, Counter] = {}
        for record in records:
            metric = by_plugin.setdefault(record.plugin, Counter())
            metric["requests"] += 1
            metric["total_tokens"] += record.total_tokens
        return {
            "settlements": [{'plugin':p,'unit':u,'dimension':d,'cost':str(v)} for (p,u,d),v in settlements.items()],
            "settlement_state": 'mixed' if len(settlements)>1 else 'known' if settlements else 'unknown',
            "unclassified_settlements": unclassified,
            "totals": {**totals, **({"vendor_cost": float(next(iter(settlements.values()))) if len(settlements)==1 and not unclassified else None} if settlements or unclassified else {}), "success_rate": round(totals["successes"] / totals["requests"] * 100, 1) if totals["requests"] else 100.0},
            "plugins": by_plugin,
            "records": [asdict(record) for record in sorted(records, key=lambda item: item.timestamp, reverse=True)[:max(1, min(limit, 500))]],
        }
