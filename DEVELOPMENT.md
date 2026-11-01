# 开发指南

## 分层边界

```text
HTTP 客户端
   │
   ▼
src/kernel/nachuan/app.py          OpenAI 兼容入口、鉴权、SSE 输出
   │
   ▼
src/kernel/nachuan/kernel.py       路由、生命周期、账号租约、并发许可
   │  NDJSON JSON-RPC over stdin/stdout
   ▼
src/plugin-runtime/src/host.ts     插件进程宿主、方法分发、取消
   │
   ▼
src/plugins/*/index.ts             平台业务协议与上游请求
```

**边界规则：**

- Python 只负责平台无关的宿主能力，不写平台名称、平台鉴权或平台协议转换。
- TypeScript 负责平台鉴权、上游请求、SSE 解析、工具调用与业务转换。
- 内核通过 `plugin.init`、`chat.completion`、`chat.stream`、`credential.health`、`credential.refresh`、`task.execute` 与插件通信。
- 插件进程崩溃不会拖垮 Python 宿主；插件会被摘除路由，需显式重载恢复。

## 启动顺序

1. 构建 `src/plugin-runtime`，生成 `dist/host.js`。
2. 启动 Python 内核，读取 `src/backend/config.local.json`。
3. 内核扫描 `src/plugins/*/spec.json`；协议 2 插件可无账号协商能力，无账号时不会发布聊天路由。
4. 控制台通过 Vite 代理访问 `/kernel`、`/v1` 和 `/health`。

没有账号的插件会显示 `credential_missing`，不会产生模型路由。这是正确行为，不是启动失败。

## 配置

示例配置：`examples/config.example.json`。

可热更新字段：

- `requestTimeoutSeconds`
- `healthIntervalSeconds`
- `maxRequestBytes`
- `fallbackEnabled`
- `routes`
- `plugins.*.enabled` / `plugins.*.maxInFlight`

调用：

```http
PUT /kernel/config
Authorization: Bearer <api_key>
Content-Type: application/json
```

配置先写临时文件再原子替换；应用失败会恢复旧配置。不要直接编辑正在运行的 `config.local.json`。

## 账号与失败语义

- 每个账号一个凭证 JSON 文件。
- 同一账号可配置 `maxInFlight`；插件级 `maxInFlight` 是总闸门。
- `429`：账号冷却后切换其他账号。
- `401/403`：标记 `reauth_required`；协商了刷新 handler 且账号允许刷新时，后续巡检会尝试刷新，否则需要更新凭证。
- `402`：额度耗尽。
- 连续传输错误：账号熔断，到期恢复。
- 客户端取消：释放账号租约，不惩罚账号。
- 流式请求在首个数据块前失败：返回真实 HTTP 错误；首块后失败：只能通过 SSE `error` 事件报告。

## 新增插件

1. 创建目录：

```text
src/plugins/2api-plugin-example/
├── spec.json
├── index.ts
└── credentials/
```

2. `spec.json` 至少声明：

```json
{
  "name": "2api-plugin-example",
  "version": "1.0.0",
  "runtime": { "entry": "index.ts", "language": "typescript", "protocol": 1 },
  "models": [{ "id": "example-model", "upstreamId": "upstream-model" }],
  "config": { "baseUrl": "https://example.invalid", "chatPath": "/v1/chat/completions" }
}
```

3. `index.ts` 导出 `PluginFactory`。OpenAI 兼容上游可以直接复用 `openAIBusiness`；私有协议必须自己实现 `completion`、`stream`，并接受 `AbortSignal`。

```ts
import { openAIBusiness } from '../../plugin-runtime/dist/openai.js'

export default openAIBusiness([{ id: 'example-model' }])
```

4. 构建运行时后重启或调用 `POST /kernel/reload`。
5. 添加账号凭证，确认 `/v1/models` 出现 `<prefix>:<model>` 路由。

## 测试

```bash
pnpm test
```

等价于：

```bash
pnpm --dir src/plugin-runtime build
(cd src/kernel && python3 -m unittest discover -s tests -v)
pnpm --dir src/plugin-runtime test
pnpm --dir src/console test
pnpm --dir src/console build
```

内核测试使用真实 Node 子进程和本地 aiohttp 假上游。覆盖：

- 非流式与 SSE 流式
- 账号故障转移
- 插件崩溃摘除与重载
- 流式取消释放账号和插件许可
- 首块前流式失败返回 HTTP 错误
- 用量块只输出一次
- 热配置回滚
- 任务调度去重
- 账本并发守恒

## 调试

- 内核日志：标准输出，包含插件启动、隔离、重载和请求访问日志。
- 插件 stderr：由 Python 子进程捕获，在插件退出时附加到错误信息。
- 运行状态：`GET /kernel/status`
- 插件状态：`GET /kernel/plugins`
- 监控：`GET /kernel/monitor`
- 账本：`GET /kernel/ledger`
- 调度：`GET /kernel/tasks`
- 单插件连接检查：`POST /kernel/plugins/{name}/health/check`，只探测一个已启用账号，不刷新凭证或运行其他插件的巡检。

所有 `/kernel/*` 接口都需要 `Authorization: Bearer <api_key>`。

进程协议单行消息上限为 64 MiB；超出上限将隔离插件并失败在途请求，需显式重载。HTTP 请求大小限制则按当前热配置对每个新请求生效。

## 内核 V2 开发

新增插件以 specVersion:2 声明 credentialSchema/settingsSchema、models[].capabilities/optionsSchema、actions 和 quota。实际 handler 在 plugin.init 握手中协商，不能以静态声明代替实现。参考 `examples/plugin-v2.spec.example.json` 和 `src/kernel/tests/fixtures/plugin-v2/index.ts`。验收与 API 见 `docs/内核V2验收记录_2026-10-31.md`。

多个账号执行同一周期动作时，每项任务声明独立 id，以及 accountId/input。options 对象及顶层已声明 vendor 字段经模型 optionsSchema 校验。协议 SDK 仅实现 nachuan-schema/1 的受限子集，Python/TS 共用 `src/contracts/schema-fixtures.json`。

## V2.1 兼容与恢复

Schema 子集增加有界 anyOf，用于默认凭证的 apiKey/accessToken 二选一校验；Python/TS 共用 fixture。新插件自定义 credentialSchema 的校验保持不变。

账号 POST/PATCH 支持 refreshEnabled；未设置时按运行时实际协商的 credentialRefresh 能力启用，显式 false 会持久化并跨重载保留。

动作历史损坏、不可读或在初始化标记存在时丢失，将返回 503 action_history_unavailable；/kernel/status 的 actionHistory 提供诊断。恢复可信历史及初始化标记后重启宿主，不能删除幂等记录后自动重试。升级会丢弃旧 redactionVersion 的结果副本，保留动作身份、终态和幂等键；新结果标记 redactionVersion=5。

动作结果对已登记的数字、布尔和空值凭证及常见文本形式做保守遮盖；相同的普通值也可能被遮盖，键名遮盖后发生冲突时动作结果会报错，副作用动作标记 unknown。值匹配无法防范插件对秘密进行任意编码或转换，插件仍需遵守不输出凭证的契约。
