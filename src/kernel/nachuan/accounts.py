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
import weakref
from . import credentials
from .schema import LEGACY_CREDENTIAL

log = logging.getLogger("nachuan.accounts")
_LIVE_ACCOUNTS = weakref.WeakValueDictionary()


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


@dataclass(slots=True, weakref_slot=True)
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
    refresh_enabled: bool | None = None

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("credential_file")
        value["credentialConfigured"]=self.credential_file.is_file()
        value["generation"]=self.credential_generation
        return value

    def credential(self) -> dict[str, Any]:
        value = credentials.read(self.credential_file)["secret"]
        if not isinstance(value, dict):
            raise ValueError("凭证文件必须为 JSON 对象")
        return value


@dataclass(frozen=True, slots=True)
class Lease:
    account: Account
    release: Callable[[Failure | None], Any]


class AccountPool:
    def __init__(self, directory: str | Path, *, breaker_threshold: int = 3, breaker_seconds: float = 300, random_source: random.Random | None = None, credential_schema: dict | None = None):
        self.credential_schema = credential_schema or LEGACY_CREDENTIAL
        self.directory = Path(directory)
        self.breaker_threshold = breaker_threshold
        self.breaker_seconds = breaker_seconds
        self.random = random_source or random.Random()
        self._accounts: dict[str, Account] = {}
        self._lock = credentials.lock_for(self.directory)
        self._dirty = False
        self._save_lock = asyncio.Lock()
        self._saver: asyncio.Task[None] | None = None
        self._closed = False

    def load(self) -> None:
        self._accounts.clear()
        restored=set()
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.json")):
            if path.name == "state.json":
                continue
            try:
                envelope = credentials.read(path)
                from .schema import validate
                validate(envelope['secret'],self.credential_schema)
                raw = {**envelope['account'], 'generation':envelope['generation'], 'lastRefreshAt':envelope.get('updatedAt',0)}
                if not isinstance(raw, dict):
                    continue
                key=str(path.resolve())
                account=_LIVE_ACCOUNTS.get(key)
                if account is None:
                    account=Account(path.stem, path, bool(raw.get("enabled", True)), max_in_flight=max(1, int(raw.get("maxInFlight", 2))), credential_generation=int(raw.get("generation", 1)), last_refresh_at=float(raw.get("lastRefreshAt", 0)), refresh_supported=bool(raw.get("refreshSupported", False)))
                    _LIVE_ACCOUNTS[key]=account
                    restored.add(path.stem)
                account.enabled=bool(raw.get('enabled',True))
                account.max_in_flight=max(1,int(raw.get('maxInFlight',2)))
                account.credential_generation=int(raw.get('generation',1))
                account.refresh_enabled=raw.get('refreshEnabled')
                if not account.enabled: account.state=AccountState.DISABLED
                elif account.state==AccountState.DISABLED: account.state=AccountState.READY
                self._accounts[path.stem]=account
            except (OSError, ValueError) as exc:
                log.warning("跳过不可读凭证 %s: %s", path.name, exc)
        state_path = self.directory / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                scrubbed=False
                for saved in state.get('accounts',{}).values():
                    if isinstance(saved,dict) and saved.get('reason'):
                        saved['reason']='previous_failure'; scrubbed=True
                if scrubbed: credentials.atomic(state_path,state)
                for uid, saved in state.get("accounts", {}).items():
                    if uid in restored and (account := self._accounts.get(uid)):
                        account.state = AccountState(saved.get("state", AccountState.READY)) if account.enabled else AccountState.DISABLED
                        if account.enabled and account.state == AccountState.DISABLED: account.state=AccountState.READY
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
                live={account.id:account for account in list(_LIVE_ACCOUNTS.values()) if account.credential_file.parent.resolve()==self.directory.resolve() and account.credential_file.exists()}
                payload = {"accounts": {uid: account.snapshot() for uid, account in live.items()}}
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
        if account.state in {AccountState.COOLING, AccountState.EXHAUSTED, AccountState.CIRCUIT_OPEN} and account.until > 0 and account.until <= now:
            account.state = AccountState.READY
            account.reason = ""
            account.until = 0
            account.failures = 0
            self._dirty = True

    def _available(self, now: float) -> list[Account]:
        result = []
        for account in self._accounts.values():
            try:
                envelope=credentials.read(account.credential_file)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            account.enabled=envelope['account']['enabled']
            account.max_in_flight=envelope['account']['maxInFlight']
            account.credential_generation=envelope['generation']
            if not account.enabled: account.state=AccountState.DISABLED
            elif account.state==AccountState.DISABLED: account.state=AccountState.READY
            self._refresh(account, now)
            if account.enabled and account.state == AccountState.READY and account.in_flight < account.max_in_flight:
                result.append(account)
        result.sort(key=lambda item: (item.in_flight / item.max_in_flight, item.last_used, item.id))
        return result

    async def acquire(self, *, exclude=None) -> Lease:
        async with self._lock:
            candidates = [a for a in self._available(time.time()) if a.id not in (exclude or set())]
            if not candidates:
                raise RuntimeError("没有可用账号")
            # 最优的两个账号中加权随机，避免持续固定打向同一凭证。
            account = self.random.choice(candidates[:2])
            account.in_flight += 1
            account.total_requests += 1
            account.last_used = time.time()
            self._dirty = True
        generation=account.credential_generation
        return Lease(account, lambda failure: self.release(account.id, failure, generation))

    async def release(self, uid: str, failure: Failure | None, generation: int | None = None) -> None:
        async with self._lock:
            account = self._accounts[uid]
            account.in_flight = max(0, account.in_flight - 1)
            if generation is not None and generation != account.credential_generation:
                return
            if failure is None:
                account.failures = 0
                account.reason = ""
                if account.state in {AccountState.CIRCUIT_OPEN, AccountState.COOLING}:
                    account.state = AccountState.READY
            else:
                account.total_failures += 1
                account.reason = failure.kind.value
                if failure.kind == FailureKind.REAUTH:
                    account.state = AccountState.REAUTH_REQUIRED
                elif failure.kind == FailureKind.RATE_LIMIT:
                    account.state = AccountState.COOLING
                    account.until = time.time() + min(max(failure.retry_after, 1), 86400)
                elif failure.kind == FailureKind.EXHAUSTED:
                    account.state = AccountState.EXHAUSTED
                    account.until = time.time() + min(failure.retry_after,86400) if failure.retry_after > 0 else 0
                elif failure.kind in {FailureKind.TRANSIENT, FailureKind.UNKNOWN}:
                    account.failures += 1
                    if account.failures >= self.breaker_threshold:
                        account.state = AccountState.CIRCUIT_OPEN
                        account.until = time.time() + self.breaker_seconds
                self._dirty = True

    async def add(self, uid: str, credential: dict[str, Any], *, max_in_flight: int = 2, enabled: bool = True, refresh_enabled: bool | None = None) -> None:
        credentials.account_id(uid)
        if refresh_enabled is not None and type(refresh_enabled) is not bool: raise ValueError('invalid refreshEnabled')
        if type(max_in_flight) is not int or not 1 <= max_in_flight <= 1000 or type(enabled) is not bool:
            raise ValueError("invalid account settings")
        async with self._lock:
            path = self.directory / f"{uid}.json"
            envelope = {'formatVersion':2,'generation':1,'updatedAt':time.time(),
                        'account':{'enabled':enabled,'maxInFlight':max_in_flight,**({'refreshEnabled':refresh_enabled} if refresh_enabled is not None else {})},'secret':credential}
            credentials.write(path,envelope,self.credential_schema,create=True)
            self._accounts[uid] = Account(uid,path,enabled,state=AccountState.READY if enabled else AccountState.DISABLED,max_in_flight=max_in_flight,refresh_enabled=refresh_enabled)
            _LIVE_ACCOUNTS[str(path.resolve())]=self._accounts[uid]
            self._dirty=True

    async def rotate(self, uid: str, credential: dict[str, Any], *, expected_generation: int | None = None, recovered: bool = False, replace: bool = False) -> None:
        async with self._lock:
            account = self._accounts[uid]
            envelope = credentials.read(account.credential_file)
            generation=envelope['generation']
            if expected_generation is not None and generation != expected_generation:
                raise credentials.GenerationConflict("credential generation conflict")
            envelope['secret'] = credential if replace else {**envelope['secret'],**credential}
            envelope['generation']=generation+1
            envelope['updatedAt']=time.time()
            credentials.write(account.credential_file,envelope,self.credential_schema)
            account.credential_generation=generation+1
            account.last_refresh_at=envelope['updatedAt']
            if recovered and account.state != AccountState.DISABLED:
                account.state=AccountState.READY if account.enabled else AccountState.DISABLED
                account.reason=""
                account.failures=0
            self._dirty=True

    async def patch(self, uid: str, *, enabled: bool | None = None, max_in_flight: int | None = None, refresh_enabled: bool | None = None) -> None:
        if refresh_enabled is not None and type(refresh_enabled) is not bool: raise ValueError('invalid refreshEnabled')
        if enabled is not None and type(enabled) is not bool: raise ValueError("enabled must be boolean")
        if max_in_flight is not None and (type(max_in_flight) is not int or not 1 <= max_in_flight <= 1000): raise ValueError("invalid maxInFlight")
        async with self._lock:
            account=self._accounts[uid]
            envelope=credentials.read(account.credential_file)
            if enabled is not None:
                envelope['account']['enabled']=enabled
                account.enabled=enabled
                if not enabled: account.state=AccountState.DISABLED
                elif account.state == AccountState.DISABLED: account.state=AccountState.READY
            if max_in_flight is not None:
                envelope['account']['maxInFlight']=max_in_flight
                account.max_in_flight=max_in_flight
            if refresh_enabled is not None:
                envelope['account']['refreshEnabled']=refresh_enabled
            credentials.write(account.credential_file,envelope,self.credential_schema)
            account.refresh_enabled=envelope['account'].get('refreshEnabled')
            if account.refresh_enabled is False: account.refresh_supported=False
            self._dirty=True

    async def set_enabled(self, uid: str, enabled: bool) -> None:
        await self.patch(uid,enabled=enabled)

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
