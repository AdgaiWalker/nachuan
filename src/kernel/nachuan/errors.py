"""跨语言错误的稳定表示，避免用字符串猜测平台语义。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from .accounts import Failure, FailureKind


@dataclass(slots=True)
class PluginFailure(RuntimeError):
    kind: FailureKind
    message: str
    retry_after: float = 0

    def __str__(self) -> str:
        return self.message

    def account_failure(self) -> Failure | None:
        if self.kind == FailureKind.INVALID_REQUEST:
            return None
        return Failure(self.kind, self.retry_after, self.message)


def as_failure(error: BaseException) -> PluginFailure:
    if isinstance(error, PluginFailure):
        return error
    return PluginFailure(FailureKind.UNKNOWN, str(error))


def failure_from_rpc(error: dict[str, Any]) -> PluginFailure:
    data = error.get("data") or {}
    try:
        kind = FailureKind(data.get("kind", "unknown"))
    except ValueError:
        kind = FailureKind.UNKNOWN
    return PluginFailure(kind, str(error.get("message", "插件调用失败")), float(data.get("retryAfter", 0)))
