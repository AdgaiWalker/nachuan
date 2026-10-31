"""宿主与业务插件的契约；不包含任何平台业务知识。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
import re

PROTOCOL_VERSION = 1
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")


class ContractError(ValueError):
    """业务插件契约不可装载。"""


class PluginUnavailable(RuntimeError):
    """插件不可用或并发已满。"""


class Lifecycle(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    DISABLED = "disabled"
    CREDENTIAL_MISSING = "credential_missing"
    FAULT = "fault"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    upstream_id: str
    max_tokens: int = 0


@dataclass(frozen=True, slots=True)
class PluginSpec:
    name: str
    version: str
    entry: str
    models: tuple[Model, ...]
    settings: dict[str, Any] = field(default_factory=dict)
    tasks: tuple[dict[str, Any], ...] = ()

    @property
    def prefix(self) -> str:
        return self.name.removeprefix("2api-plugin-")


@dataclass(frozen=True, slots=True)
class PluginSetting:
    enabled: bool = True
    max_in_flight: int = 0


def validate_spec(raw: dict[str, Any], directory: str) -> PluginSpec:
    if not isinstance(raw, dict):
        raise ContractError("spec.json 必须是对象")
    name = raw.get("name", "")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ContractError("name 须为小写字母、数字或连字符")
    if not isinstance(raw.get("version"), str) or not raw["version"]:
        raise ContractError("version 不能为空")
    entry = raw.get("runtime", {}).get("entry", "index.ts")
    if not isinstance(entry, str) or not entry.endswith(".ts") or entry.startswith("/") or ".." in entry.split("/"):
        raise ContractError("runtime.entry 须为插件目录内的 TypeScript 文件")
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise ContractError("models 至少声明一个模型")
    parsed: list[Model] = []
    seen: set[str] = set()
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _MODEL.fullmatch(item["id"]):
            raise ContractError("模型 ID 不合法")
        if item["id"] in seen:
            raise ContractError(f"模型 ID 重复: {item['id']}")
        seen.add(item["id"])
        upstream_id = item.get("upstreamId", item["id"])
        if not isinstance(upstream_id, str) or not upstream_id:
            raise ContractError("upstreamId 不能为空")
        max_tokens = item.get("maxTokens", 0)
        if not isinstance(max_tokens, int) or max_tokens < 0:
            raise ContractError("maxTokens 不能为负数")
        parsed.append(Model(item["id"], upstream_id, max_tokens))
    settings = raw.get("config", {})
    if not isinstance(settings, dict):
        raise ContractError("config 必须是对象")
    base_url = settings.get("baseUrl", raw.get("upstream", {}).get("baseUrl", ""))
    if base_url:
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username:
            raise ContractError("baseUrl 须为不含凭证的 http(s) 地址")
    tasks = raw.get("tasks", [])
    if not isinstance(tasks, list):
        raise ContractError("tasks 必须是数组")
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("action"), str) or not _NAME.fullmatch(task["action"]):
            raise ContractError("任务 action 不合法")
        if not isinstance(task.get("intervalSeconds"), int) or task["intervalSeconds"] < 15:
            raise ContractError("任务 intervalSeconds 不得小于 15")
    return PluginSpec(name, raw["version"], entry, tuple(parsed), settings, tuple(tasks))
