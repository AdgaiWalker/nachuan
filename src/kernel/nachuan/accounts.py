"""通用账号池：凭证目录、租约、冷却、熔断与安全快照。

平台错误由 TypeScript 业务插件归一化后传入；本模块不识别平台名称。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable
import asyncio
import json
import logging
import os
import random
import time

log = logging.getLogger("nachuan.accounts")


class AccountState(StrEnum):
    READY = "ready"
    DISABLED = "disabled"
    REAUTH_REQUIRED = "reauth_required"
    COOLING = "cooling"
    EXHAUSTED = "exhausted"
    CIRCUIT_OPEN = "circuit_open"


class FailureKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    REAUTH = "reauth"
    EXHAUSTED = "exhausted"
    TRANSIENT = "transient"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Failure:
    kind: FailureKind
    retry_after: float = 0
    message: str = ""


@dataclass(slots=True)
class Account:
    id: str
    credential_file: Path
    enabled: bool = field(default=True, compare=False)
    state: AccountState = AccountState.READY
    reason: str = ""
    until: float = 0
    failures: int = 0
    in_flight: int = 0
    max_in_flight: int = 2
    total_requests: int = 0
    total_failures: int = 0
    last_used: float = 0
    credential_generation: int = 1
    last_refresh_at: float = 0
    refresh_supported: bool = False

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("credential_file")
        return value

    def credential(self) -> dict[str, Any]:
        value = json.loads(self.credential_file.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("凭证文件必须为 JSON 对象")
        return value


@dataclass(frozen=True, slots=True)
class Lease:
    account: Account
    release: Callable[[Failure | None], Any]


class AccountPool:
    def __init__(self, directory: str | Path, *, breaker_threshold: int = 3, breaker_seconds: float = 300, random_source: random.Random | None = None):
        self.directory = Path(directory)
        self.breaker_threshold = breaker_threshold
        self.breaker_seconds = breaker_seconds
        self.random = random_source or random.Random()
        self._accounts: dict[str, Account] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._save_lock = asyncio.Lock()
        self._saver: asyncio.Task[None] | None = None
        self._closed = False

    def load(self) -> None:
        self._accounts.clear()
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.json")):
            if path.name == "state.json":
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                self._accounts[path.stem] = Account(path.stem, path, bool(raw.get("enabled", True)), max_in_flight=max(1, int(raw.get("maxInFlight", 2))), credential_generation=int(raw.get("generation", 1)), last_refresh_at=float(raw.get("lastRefreshAt", 0)), refresh_supported=bool(raw.get("refreshSupported", False)))
            except (OSError, ValueError) as exc:
                log.warning("跳过不可读凭证 %s: %s", path.name, exc)
        state_path = self.directory / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                for uid, saved in state.get("accounts", {}).items():
                    if account := self._accounts.get(uid):
                        account.state = AccountState(saved.get("state", AccountState.READY)) if account.enabled else AccountState.DISABLED
                        account.reason = str(saved.get("reason", ""))
                        account.until = float(saved.get("until", 0))
                        account.failures = int(saved.get("failures", 0))
                        account.total_requests = int(saved.get("total_requests", 0))
                        account.total_failures = int(saved.get("total_failures", 0))
            except (OSError, ValueError) as exc:
                log.warning("账号池状态不可读，使用凭证默认状态: %s", exc)

    def start(self) -> None:
        if self._saver is None:
            self._saver = asyncio.create_task(self._save_loop())

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self._dirty:
                await self.save()

    async def save(self) -> None:
        async with self._save_lock:
            async with self._lock:
                payload = {"accounts": {uid: account.snapshot() for uid, account in self._accounts.items()}}
                self._dirty = False
            self.directory.mkdir(parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            target = self.directory / "state.json"
            temporary = self.directory / "state.json.tmp"
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(target)
            target.chmod(0o600)

    def _refresh(self, account: Account, now: float) -> None:
        if account.state in {AccountState.COOLING, AccountState.EXHAUSTED, AccountState.CIRCUIT_OPEN} and account.until <= now:
            account.state = AccountState.READY
            account.reason = ""
            account.until = 0
            account.failures = 0
            self._dirty = True

    def _available(self, now: float) -> list[Account]:
        result = []
        for account in self._accounts.values():
            self._refresh(account, now)
            if account.enabled and account.state == AccountState.READY and account.in_flight < account.max_in_flight:
                result.append(account)
        result.sort(key=lambda item: (item.in_flight / item.max_in_flight, item.last_used, item.id))
        return result

    async def acquire(self) -> Lease:
        async with self._lock:
            candidates = self._available(time.time())
            if not candidates:
                raise RuntimeError("没有可用账号")
            # 最优的两个账号中加权随机，避免持续固定打向同一凭证。
            account = self.random.choice(candidates[:2])
            account.in_flight += 1
            account.total_requests += 1
            account.last_used = time.time()
            self._dirty = True
        return Lease(account, lambda failure: self.release(account.id, failure))

    async def release(self, uid: str, failure: Failure | None) -> None:
        async with self._lock:
            account = self._accounts[uid]
            account.in_flight = max(0, account.in_flight - 1)
            if failure is None:
                account.failures = 0
                account.reason = ""
                if account.state in {AccountState.CIRCUIT_OPEN, AccountState.COOLING}:
                    account.state = AccountState.READY
            else:
                account.total_failures += 1
                account.reason = failure.message
                if failure.kind == FailureKind.REAUTH:
                    account.state = AccountState.REAUTH_REQUIRED
                elif failure.kind == FailureKind.RATE_LIMIT:
                    account.state = AccountState.COOLING
                    account.until = time.time() + min(max(failure.retry_after, 1), 86400)
                elif failure.kind == FailureKind.EXHAUSTED:
                    account.state = AccountState.EXHAUSTED
                    account.until = time.time() + min(max(failure.retry_after, 3600), 86400)
                elif failure.kind in {FailureKind.TRANSIENT, FailureKind.UNKNOWN}:
                    account.failures += 1
                    if account.failures >= self.breaker_threshold:
                        account.state = AccountState.CIRCUIT_OPEN
                        account.until = time.time() + self.breaker_seconds
                self._dirty = True

    async def add(self, uid: str, credential: dict[str, Any], *, max_in_flight: int = 2) -> None:
        if not uid or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in uid):
            raise ValueError("账号标识仅支持字母、数字、点、下划线与连字符")
        if not isinstance(credential.get("apiKey"), str) or not credential["apiKey"]:
            raise ValueError("apiKey 不能为空")
        path = self.directory / f"{uid}.json"
        if path.exists():
            raise FileExistsError("账号已存在")
        payload = {key: value for key, value in credential.items() if key not in {"state", "reason", "until"}}
        payload.update(enabled=True, maxInFlight=max_in_flight)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        temporary = self.directory / f".{uid}.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
        async with self._lock:
            self._accounts[uid] = Account(uid, path, max_in_flight=max_in_flight)
            self._dirty = True

    async def rotate(self, uid: str, credential: dict[str, Any], *, expected_generation: int | None = None, recovered: bool = False) -> None:
        if uid not in self._accounts:
            raise KeyError(uid)
        if not isinstance(credential.get("apiKey", credential.get("accessToken")), str) or not credential.get("apiKey", credential.get("accessToken")):
            raise ValueError("新凭证缺少 apiKey 或 accessToken")
        async with self._lock:
            account = self._accounts[uid]
            if expected_generation is not None and account.credential_generation != expected_generation:
                raise RuntimeError("凭证已被其他流程更新，放弃过期刷新结果")
            current = account.credential()
            current.update(credential)
            current["generation"] = account.credential_generation + 1
            current["lastRefreshAt"] = time.time()
            temporary = account.credential_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, account.credential_file)
            account.credential_file.chmod(0o600)
            account.credential_generation += 1
            account.last_refresh_at = time.time()
            if recovered and account.state == AccountState.REAUTH_REQUIRED:
                account.state = AccountState.READY
                account.reason = ""
                account.failures = 0
            self._dirty = True

    async def set_enabled(self, uid: str, enabled: bool) -> None:
        async with self._lock:
            if uid not in self._accounts:
                raise KeyError(uid)
            self._accounts[uid].enabled = enabled
            self._dirty = True

    def snapshot(self) -> list[dict[str, Any]]:
        return [account.snapshot() for _, account in sorted(self._accounts.items())]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._saver:
            self._saver.cancel()
            try:
                await self._saver
            except asyncio.CancelledError:
                pass
        await self.save()
