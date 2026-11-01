"""宿主与业务插件的契约；不包含任何平台业务知识。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
import re
from .schema import check_schema, validate, OBJECT, LEGACY_CREDENTIAL

PROTOCOL_VERSION = 1
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")
_ACTION = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
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
    capabilities: tuple[str, ...] = ()
    options_schema: dict = field(default_factory=lambda: dict(OBJECT))


@dataclass(frozen=True, slots=True)
class PluginSpec:
    name: str
    version: str
    entry: str
    models: tuple[Model, ...]
    settings: dict[str, Any] = field(default_factory=dict)
    tasks: tuple[dict[str, Any], ...] = ()

    spec_version: int = 1
    credential_schema: dict = field(default_factory=lambda: dict(LEGACY_CREDENTIAL))
    settings_schema: dict = field(default_factory=lambda: dict(OBJECT))
    actions: tuple[dict, ...] = ()
    quota: dict = field(default_factory=dict)

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
    if not isinstance(raw.get('runtime', {}), dict): raise ContractError('runtime must be object')
    if not isinstance(raw.get('upstream', {}), dict): raise ContractError('upstream must be object')
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
        options = item.get("optionsSchema", dict(OBJECT))
        check_schema(options)
        capabilities = item.get("capabilities", [])
        if not isinstance(capabilities,list) or any(not isinstance(x,str) for x in capabilities): raise ContractError("invalid capabilities")
        parsed.append(Model(item["id"], upstream_id, max_tokens, tuple(capabilities), options))
    settings = raw.get("config", {})
    if not isinstance(settings, dict):
        raise ContractError("config 必须是对象")
    base_url = settings.get("baseUrl", raw.get("upstream", {}).get("baseUrl", ""))
    if not isinstance(base_url, str): raise ContractError('baseUrl must be string')
    if base_url:
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username:
            raise ContractError("baseUrl 须为不含凭证的 http(s) 地址")
    spec_version = raw.get("specVersion",1)
    if type(spec_version) is not int or spec_version not in (1,2): raise ContractError("unsupported specVersion")
    credential_schema = raw.get("credentialSchema", dict(LEGACY_CREDENTIAL))
    settings_schema = raw.get("settingsSchema", dict(OBJECT))
    for schema in (credential_schema,settings_schema):
        check_schema(schema)
        if schema['type'] != 'object': raise ContractError("object schema required")
    validate(settings,settings_schema)
    actions = raw.get("actions",[]) if spec_version == 2 else []
    if not isinstance(actions,list): raise ContractError("actions must be array")
    seen_actions = set()
    for action in actions:
        if not isinstance(action,dict) or not isinstance(action.get('id'),str) or not _ACTION.fullmatch(action['id']) or action['id'] in seen_actions: raise ContractError("invalid/duplicate action")
        seen_actions.add(action['id'])
        if action.get('scope','plugin') not in ('plugin','account'): raise ContractError("invalid action scope")
        timeout=action.get('timeoutSeconds',30)
        if type(timeout) not in (int,float) or not 1 <= timeout <= 3600: raise ContractError("invalid action timeout")
        for key in ('inputSchema','outputSchema'):
            check_schema(action.get(key,dict(OBJECT)))
        for key in ('mutatesCredentials','required','allowDisabled'):
            if key in action and type(action[key]) is not bool: raise ContractError("invalid action flag")
        if action.get('idempotency','read') not in ('read','side_effect'): raise ContractError("invalid idempotency")
        if action.get('mutatesCredentials') and action.get('scope') != 'account': raise ContractError("credential mutation requires account scope")
        if action.get('allowDisabled') and not action.get('mutatesCredentials'): raise ContractError('allowDisabled requires credential management')
        if 'concurrencyKey' in action and (not isinstance(action['concurrencyKey'],str) or not action['concurrencyKey']): raise ContractError("invalid concurrency key")
    quota = raw.get('quota',{})
    if not isinstance(quota,dict): raise ContractError("invalid quota")
    if type(quota.get('query',False)) is not bool or type(quota.get('reliableExhaustion',False)) is not bool: raise ContractError("invalid quota flags")
    if type(quota.get('ttlSeconds',300)) not in (int,float) or not 1 <= quota.get('ttlSeconds',300) <= 86400: raise ContractError("invalid quota ttl")
    tasks = raw.get("tasks", [])
    if not isinstance(tasks, list):
        raise ContractError("tasks 必须是数组")
    seen_tasks=set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("action"), str) or not _ACTION.fullmatch(task["action"]):
            raise ContractError("任务 action 不合法")
        if not isinstance(task.get("intervalSeconds"), int) or task["intervalSeconds"] < 15:
            raise ContractError("任务 intervalSeconds 不得小于 15")
        task_id=task.get('id',task['action'])
        if not isinstance(task_id,str) or not _ACTION.fullmatch(task_id) or task_id in seen_tasks: raise ContractError('invalid/duplicate task id')
        seen_tasks.add(task_id)
        if task.get('accountId') is not None:
            from .credentials import account_id
            account_id(task['accountId'])
        if not isinstance(task.get('input',{}),dict): raise ContractError('task input must be object')
        if spec_version == 2:
            if task['action'] not in seen_actions: raise ContractError('task action not declared')
            action=next(a for a in actions if a['id']==task['action'])
            if action.get('scope','plugin')=='account' and not task.get('accountId'): raise ContractError('task accountId required')
            validate(task.get('input',{}),action.get('inputSchema',OBJECT))
    return PluginSpec(name, raw["version"], entry, tuple(parsed), settings, tuple(tasks), spec_version, credential_schema, settings_schema, tuple(actions), quota)
