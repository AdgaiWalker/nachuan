# 已知问题与未完成项

本文件是交付包的一部分。不要把下面的“未完成”理解为测试环境故障。

## 平台插件

### 1. 平台能力尚未完整接入

四个插件的 `index.ts` 均使用通用 `openAIBusiness` 包装：

- Gemini 的 search 上游路径不是 OpenAI chat completions。
- HelloGML 需要网页 refresh_token / cookie 协议。
- WorkBuddy 通过 `127.0.0.1:7863` 已有兼容网关转发；平台账号、私有协议及额度由原网关处理，纳川只持有本地网关 API Key。手动动作可查看原网关账号缓存，但更新时间未知，缓存零值可能未知，不能作为实时额度。
- WorkBuddy 单账号暂停/恢复选号操作仅对假网关验证，尚未对真实账号执行。结果未知时必须先到原网关核对状态；登录、签到和删除仍在原面板处理。
- Qwen 的 DashScope compatible 接口最接近可联调，但仍需真实 key 验证。

WorkBuddy 桥接已做本机普通和流式聊天实测，但不满足完整平台 DoD；其他三个插件仍是开发骨架。

### 2. `spec.json` 的 `configSchema` 尚未接入通用运行时

以 Gemini 为例，`configSchema` 声明 `jwt`、`proxy_url`；当前通用运行时实际读取账号文件中的 `apiKey` 或 `accessToken`。

开发新平台插件时应：

- 要么在插件自己的 `index.ts` 中读取并校验声明的凭证字段；
- 要么统一扩展通用运行时凭证映射，不要假设 `configSchema` 已自动生效。

### 3. 平台健康检查与凭证刷新不完整

通用 `openAIBusiness` 实现 OpenAI `/models` 健康探测；WorkBuddy 在此基础上核对声明模型至少有一个出现在上游目录。`credential.refresh` 和平台特有的 `executeTask` 未实现，当前四个插件没有可执行的周期动作。WorkBuddy 的账号缓存查询是手动只读动作。

### 4. 未使用真实平台凭证验收

自动化回归使用本地假上游。WorkBuddy 另通过用户本机已有网关完成一次普通及一次流式调用；没有直接验收 WorkBuddy 私有协议，也未对 Gemini Business、HelloGML 或 Qwen 使用真实凭证。鉴权、限流、额度与错误码仍需逐平台验收。

## 内核限制

### 5. 单用户本地网关

- 只有一个共享 `api_key`。
- 没有用户体系、配额隔离、TLS 或公网防护。
- 默认只监听 `127.0.0.1`；不要直接改成 `0.0.0.0` 暴露到公网。

### 6. 账本不是强一致数据库

用量账本异步批量写入，进程被强杀时可能丢失队列中的少量记录；没有 fsync 和跨进程锁。适合本地开发和研究，不适合作为计费唯一凭证。

### 7. 流式错误的两段语义

- 首个数据块前失败：返回真实 HTTP 错误（已修复）。
- 已发送首块后失败：HTTP 状态已经是 200，只能发送 SSE `error` 事件和 `[DONE]`，客户端必须识别。通用 OpenAI 适配器要求上游发送 `[DONE]`；没有结束标记的断流按失败处理。

### 8. 平台测试受环境限制

- 已在 macOS + Python 3.14.7 + Node 24.18.0 验证。
- Windows 未验证；启动脚本是 Bash。
- Python 3.11–3.13 理论上可运行，但未做完整验收。

## 交付包范围

### 9. Go 后端与编译器未包含

原工作区的 `backend/`（Go 遗留实现）和 `2api-agent-compiler/` 未放入本包。它们不是当前 Python 内核运行链路；如需参考，请从原工作区单独获取。

### 10. 研究文档不是实现承诺

`docs/` 中的 PRD、架构报告和“已完成”描述属于研究与设计资料，部分内容早于当前 Python 内核重构。以本包 README、DEVELOPMENT 和源码为准。

## 已修复并加入回归

以下问题已在交付前修复：

- 流式取消后账号租约泄漏
- 流式首块前失败仍返回 200
- 纯 usage 块重复输出
- 禁用插件坏配置重载不能保留 last-good
- 内容块丢失 `finish_reason`
- 内容与 tool_calls 同块时内容重复

## 通用内核 V2 更新

本文件前述四平台/旧 configSchema 限制仍针对现有 legacy 骨架。V2 宿主现已提供 credentialSchema、通用账号 API、Actions 和 quota.query；接入者需实现相应 handler。内核范围、500 条动作历史的容量语义、固定间隔调度和文件存储边界见 `docs/内核V2验收记录_2026-10-31.md`。
