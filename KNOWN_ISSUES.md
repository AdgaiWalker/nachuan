# 已知问题与未完成项

本文件是交付包的一部分。不要把下面的“未完成”理解为测试环境故障。

## 平台插件

### 1. 四个插件目前不是完整平台实现

`src/plugins/2api-plugin-gemini`、`hellogml`、`qwen`、`workbuddy` 的 `index.ts` 均使用通用 `openAIBusiness` 包装：

- Gemini 的 search 上游路径不是 OpenAI chat completions。
- HelloGML 需要网页 refresh_token / cookie 协议。
- WorkBuddy 需要私有设备与聊天协议。
- Qwen 的 DashScope compatible 接口最接近可联调，但仍需真实 key 验证。

因此当前交付定位为**插件协议开发骨架**，不是四平台可用版本。

### 2. `spec.json` 的 `configSchema` 尚未接入通用运行时

以 Gemini 为例，`configSchema` 声明 `jwt`、`proxy_url`；当前通用运行时实际读取账号文件中的 `apiKey` 或 `accessToken`。

开发新平台插件时应：

- 要么在插件自己的 `index.ts` 中读取并校验声明的凭证字段；
- 要么统一扩展通用运行时凭证映射，不要假设 `configSchema` 已自动生效。

### 3. 平台健康检查与凭证刷新不完整

通用 `openAIBusiness` 只实现 OpenAI `/models` 健康探测；`credential.refresh` 和平台特有的 `executeTask` 未实现。调度器可以注册任务，但当前四个插件没有可执行的周期动作。

### 4. 未使用真实平台凭证验收

所有测试使用本地假上游；没有对真实 Gemini Business、HelloGML、Qwen、WorkBuddy 发起请求。真实上游的鉴权、限流、SSE 格式和错误码仍需逐平台验收。

## 内核限制

### 5. 单用户本地网关

- 只有一个共享 `api_key`。
- 没有用户体系、配额隔离、TLS 或公网防护。
- 默认只监听 `127.0.0.1`；不要直接改成 `0.0.0.0` 暴露到公网。

### 6. 账本不是强一致数据库

用量账本异步批量写入，进程被强杀时可能丢失队列中的少量记录；没有 fsync 和跨进程锁。适合本地开发和研究，不适合作为计费唯一凭证。

### 7. 流式错误的两段语义

- 首个数据块前失败：返回真实 HTTP 错误（已修复）。
- 已发送首块后失败：HTTP 状态已经是 200，只能发送 SSE `error` 事件和 `[DONE]`，客户端必须识别。

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
