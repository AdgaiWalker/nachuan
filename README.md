# 纳川开发交付包

这是纳川统一网关的**开发基线**，用于内核测试、插件开发和协议联调。

> **状态声明**
> - 可用：Python 宿主内核、TypeScript 插件进程协议、账号池、路由、流式归一化、监控、账本、热配置和控制台。
> - 已联调：WorkBuddy 插件可通过本机已有的 WorkBuddy 兼容网关转发聊天；独立临时内核的普通和流式调用已实测。控制台提供试用、连接检查、原网关账号缓存查看，以及逐账号暂停/恢复入口；暂停/恢复仅经假网关验证。平台账号仍由已有网关管理。
> - 未完成：其余三个平台的真实上游适配，以及 WorkBuddy 平台专属账号、额度和动作的纳川内集成。
> - 未验证：其余平台真实凭证、完整控制台与逐平台接入 DoD。
> - 非生产：单用户本地网关，不含多租户、TLS 和公网防护。

本次审查修复及后续接入事项见 `TODO.md` 和 `修复记录_2026-10-31.md`。

工作区现有的 V2.3 ZIP 是历史交付快照，不含后添加的 `AGENTS.md`、文档更新和 WorkBuddy 桥接修改；请以当前开发目录为准。

## 通用内核与当前边界

已提供协议 1/2 协商、声明式 schema、通用凭证、Actions、额度快照和分单位账本。完整范围、API、迁移及已知边界见 [内核 V2 验收记录](docs/内核V2验收记录_2026-10-31.md)。已修复复审 R01–R06；动作历史不可读时进入受限状态，旧版动作结果会清除、幂等身份保留。WorkBuddy 当前采用本地网关桥接，其余平台真实适配仍未完成。

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
src/plugins/           WorkBuddy 本地网关桥接及其他平台开发骨架
src/console/           React 控制台
src/backend/           仅存放本地配置；Go 后端未包含
examples/              配置和凭证示例
docs/                  研究与架构资料，不是运行依赖
scripts/               安装、测试和启动脚本
```

原工作区的 Go `backend/`、`2api-agent-compiler/` 和研究脚本**没有放入本交付包**，因为它们不属于当前 Python 内核运行链路。原始副本仍保留在交付方工作区。

## 账号配置

每个账号一个 JSON 文件，放在对应插件的 `credentials/` 目录。当前四个通用适配器读取 `apiKey` 或 `accessToken`；V2 插件也可以通过 `credentialSchema` 声明其他结构。推荐通过账号 API 写入。手工创建旧格式示例时，先确认目标不存在并设置私有权限：

```bash
credential_dir=src/plugins/2api-plugin-qwen/credentials
mkdir -p "$credential_dir"
chmod 700 "$credential_dir"
if [ -e "$credential_dir/account-1.json" ]; then
  echo "账号文件已存在，未覆盖"
else
  (umask 077; cp examples/credentials.example.json "$credential_dir/account-1.json")
  chmod 600 "$credential_dir/account-1.json"
fi
```

```json
{
  "apiKey": "REPLACE_WITH_UPSTREAM_API_KEY",
  "enabled": true,
  "maxInFlight": 1
}
```

宿主写入凭证时使用 `0700/0600`；手工复制或已有文件的权限不会因读取自动修正，请自行检查。不要提交到 Git。

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
| `2api-plugin-workbuddy` | 桥接本机 WorkBuddy 网关，聊天链路已实测；账号与额度仍由原服务管理 |

`spec.json` 里的 `configSchema` 是目标接入字段，不代表当前通用运行时已经消费这些字段。详见 `KNOWN_ISSUES.md`。

WorkBuddy 插件的账号密钥是 **7863 本地网关的 API Key**，并非平台登录凭证。接入与实测范围见 [平台接入实施与验收](docs/平台接入实施与验收.md)。

## 验证基线

- Python：96 项测试通过（含通用内核集成验收）
- TypeScript：29 项测试通过（含跨语言共用 schema fixtures）
- 控制台：1 项筛选/竞态回归、TypeScript 检查与 Vite 构建通过

以上为 2026-11-01 V2.3 交付包的本地验证记录，不代表此后每次运行的结果；测试不请求真实平台，使用本地假上游验证 Python → Node → HTTP 链路。

## 关键文档

- `DEVELOPMENT.md`：架构、插件开发、调试与测试方法
- `KNOWN_ISSUES.md`：已知限制和未完成项
- `SECURITY.md`：本地密钥、凭证和网络暴露边界
- `docs/`：研究与架构资料；其中描述的是设计目标和历史分析，不代表当前全部已实现
