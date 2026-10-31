# 纳川 Python 内核

**语言边界不可交换：Python 是唯一宿主内核，TypeScript 负责业务插件。Go 仅保留为遗留参考；何时重构由用户决定。**

## 结构

- `nachuan/contracts.py`：版本化契约与校验。
- `nachuan/transport.py`：NDJSON 进程适配、消息关联、超时、取消与背压。
- `nachuan/kernel.py`：生命周期、last-good、路由、并发许可、崩溃看门狗与退役排空。
- `nachuan/accounts.py`：账号租约、冷却、熔断、状态持久化。
- `nachuan/errors.py`：跨语言统一错误分类。
- `nachuan/streaming.py`：SSE 归一化、跨包思考标签与流式用量。
- `nachuan/health.py`：健康证据、凭证刷新窗口与低频巡检。
- `nachuan/config.py`：热配置、原子落盘与失败回滚。
- `nachuan/tasks.py`：周期任务注册、单实例去重与有界历史。
- `nachuan/ledger.py`：双轨用量、异步幂等结算与私有持久化。
- `nachuan/monitoring.py`：请求指标、隔离事件流、SSE 实时订阅与脱敏。
- `nachuan/app.py`：OpenAI 兼容 HTTP 入口；不含管理界面。
- `../plugin-runtime/`：TypeScript 业务插件宿主与上游适配。
- `../plugins/`：平台业务代码；内核不包含平台名称。

## 启动

```bash
cd src/plugin-runtime
pnpm install --frozen-lockfile
pnpm build
cd ../kernel
python3 -m nachuan.app --port 7865
```

## 接口

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `GET /kernel/status`
- `GET /kernel/plugins`
- `GET /kernel/monitor`：请求、成功率、Token、在途、延迟与事件。
- `GET /kernel/ledger`：双轨用量与结算记录。
- `GET|PUT /kernel/config`：热配置读取与原子发布；失败自动恢复旧配置。
- `GET /kernel/tasks` + `POST /kernel/tasks/{id}/run`：调度状态与受控手动执行。
- `POST /kernel/health/check`：低频健康巡检。
- `POST /kernel/plugins/{name}/accounts` 与 `.../accounts/{uid}/rotate`：账号接入与安全轮换。
- `POST /kernel/reload`：受控热重载。
- `POST /kernel/events/ticket` + `GET /kernel/events?ticket=...`：一次性短时订阅凭证与实时事件流。

鉴权沿用配置中的 `api_key`。插件启用与并发使用 `plugins` 对象格式：

```json
{
  "plugins": {
    "2api-plugin-qwen": { "enabled": true, "maxInFlight": 3 }
  }
}
```

旧的 `plugins` 数组格式仍可读。`config.apiKey` 是上游业务凭证，不是网关密钥；当前真实插件需要用户自行配置有效上游凭证。

## 账号凭证

每个插件的账号存放在自己的 `credentials/` 目录，例如：

```text
plugins/2api-plugin-qwen/credentials/account-1.json
```

```json
{ "apiKey": "你的上游凭证", "enabled": true, "maxInFlight": 2 }
```

凭证文件不进入内核状态快照或日志。`state.json` 仅保存健康状态，权限为 `0600`。没有账号的插件显示 `credential_missing`，不会路由请求。

- `429`：账号冷却后切换其他账号。
- `401/403`：标记 `reauth_required`，需更新凭证，不自动复活。
- `402`：额度耗尽并隔离。
- 连续传输错误：触发账号熔断，到期恢复。
- 插件崩溃：停止新路由；显式重载可恢复。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用本地假上游，真实跨越 Python → Node → HTTP，覆盖非流式、SSE、鉴权、禁用、裸模型冲突、坏契约 last-good、显式跨插件故障转移、取消释放、热配置回滚、调度去重、崩溃摘除和 100 笔并发账本守恒。不会请求真实 AI 平台。

当前验证基线：Python 44 项测试、TypeScript 4 项测试、控制台 TypeScript/Vite 构建全部通过。平台专用协议与真实凭证验收仍属于下一阶段，不纳入本次通用内核完成声明。
