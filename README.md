# 纳川开发交付包

这是纳川统一网关的**开发基线**，用于内核测试、插件开发和协议联调。

> **状态声明**
> - 可用：Python 宿主内核、TypeScript 插件进程协议、账号池、路由、流式归一化、监控、账本、热配置和控制台。
> - 未完成：四个平台插件的真实上游协议适配。当前插件只是通用 OpenAI 兼容包装，不能视为四个平台已可用。
> - 未验证：真实平台凭证与端到端上游调用。
> - 非生产：单用户本地网关，不含多租户、TLS 和公网防护。

## 运行环境

| 组件 | 已验证版本 |
|---|---|
| Python | 3.14.7 |
| aiohttp | 3.14.5 |
| Node.js | 24.18.0 |
| pnpm | 12.0.0 |

Python 需要 3.11 以上（代码使用 `StrEnum`）；其他版本未做验收。

## 快速开始

```bash
bash scripts/setup.sh
```

安装脚本会：

1. 安装 Python 依赖。
2. 安装并构建 `src/plugin-runtime`。
3. 安装 `src/console` 依赖。
4. 首次运行生成 `src/backend/config.local.json`。

然后修改 `src/backend/config.local.json` 中的：

```json
"api_key": "change-me-local-dev"
```

改成你自己的本地访问密钥。**不要使用示例值，也不要提交该文件。**

运行测试：

```bash
pnpm test
```

启动内核：

```bash
bash scripts/run-kernel.sh
```

启动控制台（另一个终端）：

```bash
bash scripts/run-console.sh
```

- 内核：`http://127.0.0.1:7865`
- 控制台：`http://127.0.0.1:5173`
- 控制台首次打开时需要输入同一个 `api_key`

## 目录

```text
src/kernel/            Python 宿主内核与测试
src/plugin-runtime/    TypeScript 插件宿主与协议适配
src/plugins/           四个平台插件（当前为开发骨架）
src/console/           React 控制台
src/backend/           仅存放本地配置；Go 后端未包含
examples/              配置和凭证示例
docs/                  研究与架构资料，不是运行依赖
scripts/               安装、测试和启动脚本
```

原工作区的 Go `backend/`、`2api-agent-compiler/` 和研究脚本**没有放入本交付包**，因为它们不属于当前 Python 内核运行链路。原始副本仍保留在交付方工作区。

## 账号配置

每个账号一个 JSON 文件，放在对应插件的 `credentials/` 目录。通用运行时当前读取 `apiKey` 或 `accessToken`：

```bash
cp examples/credentials.example.json \
  src/plugins/2api-plugin-qwen/credentials/account-1.json
```

```json
{
  "apiKey": "REPLACE_WITH_UPSTREAM_API_KEY",
  "enabled": true,
  "maxInFlight": 1
}
```

凭证目录和文件会以 `0700/0600` 权限保存。不要提交到 Git。

也可以使用控制台或接口添加：

```http
POST /kernel/plugins/2api-plugin-qwen/accounts
Authorization: Bearer <你的本地 api_key>
Content-Type: application/json

{"id":"account-1","apiKey":"<上游凭证>"}
```

### 平台插件现状

| 插件 | 当前状态 |
|---|---|
| `2api-plugin-qwen` | DashScope compatible 接口，最接近可联调状态 |
| `2api-plugin-gemini` | 通用包装，search 上游协议尚未实现 |
| `2api-plugin-hellogml` | 通用包装，网页会话协议尚未实现 |
| `2api-plugin-workbuddy` | 通用包装，WorkBuddy 私有协议尚未实现 |

`spec.json` 里的 `configSchema` 是目标接入字段，不代表当前通用运行时已经消费这些字段。详见 `KNOWN_ISSUES.md`。

## 验证基线

- Python：44 项测试通过
- TypeScript：4 项测试通过
- 控制台：TypeScript 检查与 Vite 构建通过

测试不请求真实平台；使用本地假上游验证 Python → Node → HTTP 的完整链路。

## 关键文档

- `DEVELOPMENT.md`：架构、插件开发、调试与测试方法
- `KNOWN_ISSUES.md`：已知限制和未完成项
- `SECURITY.md`：本地密钥、凭证和网络暴露边界
- `docs/`：研究与架构资料；其中描述的是设计目标和历史分析，不代表当前全部已实现
