# nachuan (纳川) · 2API 架构设计与技术实施规范 (Technical Specification)

| 项目 | 内容 |
|---|---|
| **系统名称** | **nachuan (纳川)** · 2API 微内核架构与语义转译器技术白皮书 |
| **版本** | v1.0.0 (Release Candidate) |
| **全景索引** | **[官方全景文档总索引 (DOCS_INDEX.md)](DOCS_INDEX.md)** |
| **核心命题** | **海纳百川 · 2api 即插件，单个 2api 为原子生命周期单位** |
| **底层技术栈** | **Cordis 微内核**（运行时基座） + **DeepSeek Harness**（Agent 语义编译器） |
| **文档状态** | 架构基线定稿，指导系统研发与 Agent Skill 编排 |

---

## 1. 架构愿景与核心设计哲学

### 1.1 什么是 nachuan？
`nachuan`（纳川，取“海纳百川”之意）是一个基于 **Cordis 微内核技术** 的自托管多上游反向代理网关与运维平台。它将各大 AI 服务平台（如 CodeBuddy、PhanthyCode、Cursor、Muse 等）的非官方逆向通道，标准化包装为 OpenAI 规范的 API 接口，并提供统一的 Web 管理运维面板。

### 1.2 核心设计哲学：2api 即插件（Plugin as 2API）
传统逆向代理项目往往采用“独立单体应用”模式开发，导致端口冲突、重复编写 HTTP 服务、运维碎片化。在 `2api` 体系中：
* **宿主（Host）只做微内核**：`2api-core` 仅负责加载 Cordis 上下文，提供公共网络服务、通用账号池状态机、调度器及 Web 控制台总线。
* **单个 2api 即独立插件**：每个具体的上游适配通道都是一个独立的 Cordis 插件（如 `2api-plugin-workbuddy`、`2api-plugin-phanthycode`）。
* **原子化隔离与热插拔**：每个 2api 插件拥有独立的生命周期、独立的配置 Schema、独立的账号凭证存储。单个插件的崩溃或上游风控封号被完全隔离在插件上下文中，不影响宿主和其他 2api 插件的正常运行。

### 1.3 研发方法论：双轨敏捷实验与价值重构 (Dual-Track R&D)
* **探索阶段（动态优先）**：前期不必要用 Go 编写所有代码。开发者可先用 Python/TypeScript 极速写出原型，验证上游握手、绕过策略与数据格式，零编译成本试错；
* **契约阶段（规范冻结）**：提取输出语言无关的 `ProtocolSpec` IR（`spec.json`）；
* **加固阶段（价值重构）**：评定核心价值——高并发、常驻自用的通道，再精选用 Go 静态重构进 `nachuan` 单二进制，兼顾探索极速与生产坚固。

---

## 2. 系统总体拓扑结构

系统划分为**运行时微内核（Runtime Plane）**与**Agent 语义编译面（Transpiler Plane）**：

```mermaid
flowchart TD
    subgraph DSH["Agent 语义编译面 (DeepSeek Harness)"]
        GH["GitHub 野生开源 2api 仓库"] --> Ingest["Tool: 代码摄入与 AST 静态扫描"]
        Ingest --> Pass1["Pass 1: 提取 Protocol Spec (JSON 契约)"]
        Pass1 --> Pass2["Pass 2: 发射 Cordis 插件代码 (index.ts + Schema)"]
        Pass2 --> Pass3["Pass 3: 运行沙箱构建与单元测试验证"]
        Pass3 -->|失败自愈 (Loop<=3)| Pass2
        Pass3 -->|验证全绿| OutputPkg["标准 2api 单插件包 (含 cordis.patch.yml)"]
    end

    subgraph Host["运行时微内核面 (2api Core on Cordis)"]
        OutputPkg --> Patch["cordis.patch.yml 声明式热挂载"]
        Patch --> Context["Cordis Context (微内核依赖注入容器)"]
        
        subgraph CoreServices["宿主核心注入服务 (Core Services)"]
            SRV["ctx.server\n(OpenAI 兼容端点)"]
            POOL["ctx.accountPool\n(状态机/在途租约/熔断)"]
            SCHED["ctx.scheduler\n(定时任务/每日签到)"]
            PANEL["ctx.panel\n(Schema 驱动自适应 UI)"]
        end
        
        Context --> CoreServices
        
        subgraph Plugins["2api 插件集群 (动态热装卸)"]
        P1["2api-plugin-workbuddy\n(CodeBuddy 逆向)"]
        P2["2api-plugin-phanthycode\n(PhanthyCode 逆向)"]
        P3["2api-plugin-cursor\n(Cursor 逆向)"]
        end
        
        CoreServices <-->|apply(ctx, config)| Plugins
    end
```

---

## 3. 2API 微内核（Host Core）服务契约规范

宿主内核通过 Cordis 服务扩展机制向 `Context` 注入四大核心公共服务：

```typescript
// packages/core/src/services.ts
import { Context, Service } from 'cordis'

declare module 'cordis' {
  interface Context {
    server: OpenAIServerService
    accountPool: AccountPoolService
    scheduler: SchedulerService
    panel: PanelService
    rapi: TwoApiService // 2api 核心服务注册总线
  }
}
```

### 3.1 核心服务职责定义
1. **`ctx.server`（网络接入网关）**：
   - 暴露标准的 `/v1/chat/completions`、`/v1/models` 端点。
   - 提供路由分发能力：支持显式前缀路由（如 `wb:deepseek-v3`、`pc:glm-4`）与自动模型负载均衡。
2. **`ctx.accountPool`（工业级账号池治理）**：
   - 维护多账号的健康状态机：健康（Healthy）、软冷却（Soft Cooldown，429 指数退避）、硬冷却（Hard Cooldown，余额耗尽封禁至次日）、熔断（Circuit Broken）。
   - 提供在途租约（Lease）控制并发，执行 Top-5 加权随机选号。
3. **`ctx.scheduler`（统一任务调度器）**：
   - 集中注册 Cron 排程，调度每日自动签到、连登管家、Token 保活刷新、补偿领取任务。
4. **`ctx.panel`（自适应 Web 控制台）**：
   - **Schema 驱动 UI（Schema-Driven UI）**：接收各插件导出的 Cordis `Schema`，自动渲染配置输入面板，前端免写任何胶水代码。
   - 接收各 2api 插件注册的自定义运维动作（如“一键完成成长任务”）。

---

## 4. 单个 2API 插件技术标准（Plugin Specification）

每个被集成的 2api 必须封装为一个独立的 Cordis 插件包，严格遵循以下结构：

### 4.1 目录组织规范
```
packages/plugins/2api-plugin-<target>/
├── cordis.patch.yml        # 声明式注入配置文件
├── package.json            # 插件元信息与依赖
├── src/
│   ├── config.ts           # Cordis Schema 配置定义
│   ├── client.ts           # 上游逆向出站通信与指纹伪造
│   ├── upstream-types.ts   # 上游专有数据结构
│   └── index.ts            # 插件生命周期入口 (apply)
└── tests/
    └── plugin.spec.ts      # 打桩自愈测试
```

### 4.2 声明式挂载：`cordis.patch.yml`
```yaml
# 声明本插件在 2api 运行时 profiles 层栈中的注入规则
- insert:
    - id: 2api-plugin-workbuddy
      name: '2api-plugin-workbuddy'
      config:
        enabled: true
        authDir: './auths/workbuddy'
        maxInFlight: 5
        autoCheckin: true
```

### 4.3 插件标准实现：`src/index.ts`
```typescript
import { Context, Schema } from 'cordis'
import { UpstreamClient } from './client'

export interface PluginConfig {
  enabled: boolean
  authDir: string
  softRateMax: number
  autoCheckin: boolean
}

// 核心：基于 Cordis Schema 自动生成面板配置表单，无需手动写前端页面
export const Config: Schema<PluginConfig> = Schema.object({
  enabled: Schema.boolean().default(true).description('是否启用本通道'),
  authDir: Schema.string().default('./auths/target').description('凭证文件存放目录 (0600)'),
  softRateMax: Schema.number().default(1800).description('429 软冷却最大封顶时长 (秒)'),
  autoCheckin: Schema.boolean().default(true).description('是否开启每日自动签到')
})

export function apply(ctx: Context, config: PluginConfig) {
  if (!config.enabled) return

  const client = new UpstreamClient(config)

  // 1. 向 2api 核心注册 Provider 实体
  const provider = ctx.rapi.registerProvider({
    id: 'workbuddy',
    displayName: '腾讯 CodeBuddy',
    supportedModels: ['deepseek-v3', 'glm-5.2', 'kimi-k1.5'],
    
    // 出流处理：将上游专有 SSE / DSML 标记转换为标准 OpenAI Chunk
    async chatStream(cred, req) {
      return client.requestChatStream(cred, req)
    },
    
    // 错误归一化：将上游 HTTP 错误码翻译为账号池处置动作
    classifyError(statusCode, body) {
      return client.mapErrorPolicy(statusCode, body)
    }
  })

  // 2. 挂载到统一控制台 (面板动态生成独立监控卡片)
  ctx.panel?.registerChannel({
    providerId: 'workbuddy',
    displayName: 'CodeBuddy 账号池',
    actions: [
      { id: 'growth_claim', label: '一键完成成长任务', handler: () => client.runGrowthTasks() }
    ]
  })

  // 3. 注册定时调度
  if (config.autoCheckin) {
    ctx.scheduler?.addCron('0 9,21 * * *', async () => {
      await client.runDailyCheckin()
    })
  }

  // 4. 优雅注销 (热插拔保证：卸载或配置重载时执行，彻底排空在途请求与定时器)
  ctx.on('dispose', () => {
    provider.unregister()
    client.destroy()
  })
}
```

---

## 5. 基于 DeepSeek Harness 的 Agent 编译器流水线

将野生 GitHub 仓库编译为上述标准 Cordis 插件的自动化处理流程：

```
[原始开源 Repo] 
       │
       ▼ (Pass 1)
[协议逆向语义剖析] ──> 输出 Protocol Spec JSON（严禁写业务代码）
       │
       ▼ (Pass 2)
[Cordis 插件代码发射] ──> 填入标准 Cordis Schema 与 apply(ctx) 脚手架
       │
       ▼ (Pass 3)
[物理沙箱门禁闭环] ──> DSH 执行 pnpm test / 假上游打桩 (失败自动回喂修复)
       │
       ▼ (通过)
[交付产物] ──> 输出标准插件目录及 cordis.patch.yml
```

### 5.1 Pass 1: 协议逆向提取提示词规则
* **目标**：静态扫描目标项目，提取鉴权（Auth Flow）、出站请求头（Header Spoofing）、流式协议（SSE/WS/DSML）、错误判定（Rate Limit / Quota Exhausted）。
* **产出**：结构化 JSON，包含：
  - `upstream_base_url`
  - `token_lifecycle_rules`
  - `chat_completion_endpoint`
  - `error_code_mapping`

### 5.2 Pass 2: 目标代码发射规则
* **目标**：基于 Pass 1 的 JSON 契约，自动填充预先设定的 `cordis-plugin-template`。
* **负向红线**：
  - 严禁创建独立 HTTP 端口监听（`http.createServer` / `app.listen`）。
  - 严禁引入外部未审计的网络库，一律采用宿主注入的 `ctx.http` 或标准 Fetch。
  - 必须编写伴生测试文件 `tests/plugin.spec.ts`。

### 5.3 Pass 3: 编译门禁自愈规则（Compiler Verifier）
* **沙箱执行命令**：
  ```bash
  # 1. 语法检查与 TypeScript 编译验证
  pnpm run build --filter 2api-plugin-<target>
  # 2. 单元测试与故障注入测试
  pnpm run test --filter 2api-plugin-<target>
  ```
* **自愈机制（Loop <= 3）**：若构建或打桩测试未通过，DSH 捕获控制台输出（TypeScript 诊断码或断言失败堆栈），注入提示词上下文：
  > “测试失败。请仅修复导致测试未通过的逻辑，严禁变更已定义的 `UpstreamProvider` 接口签名与 Schema 结构。”

---

## 6. 安全、隔离与运维红线

1. **凭证隔离安全**：
   - 所有上游 Token 仅持久化保存在本地沙箱，文件系统权限强制设为 `0600`。
   - 日志系统强制脱敏，严禁在控制台与请求流水中输出 accessToken / refreshToken / 会话 cookie。
2. **故障包容（Fault Containment）**：
   - 单个 2api 插件上游遭遇封锁、断网或格式漂移，只能触发该插件内部的熔断，不得阻塞 `2api` 宿主或其他 2api 插件的正常转发。
3. **自用与防滥用边界**：
   - 本项目定位于个人多账号集中管理与合规自用，严禁将生成的网关用于批量自动化注册、黑产刷量或二次售卖。

---

## 7. 实施计划与交付里程碑

| 阶段 | 周期 | 核心交付物 | 验收判据 |
|---|---|---|---|
| **M1: 宿主基座与 Cordis 契约定义** | D1~D2 | `2api-core` 微内核 + 抽象 Service 定义 | 核心能够加载 Noop 空插件，面板能基于 Schema 渲染空态表单 |
| **M2: 金标插件手工适配与规范冻结** | D3~D4 | `2api-plugin-workbuddy` 与 `2api-plugin-phanthycode` | 两个不同平台的账号可被宿主同时管理，统一端口正常响应流式补全 |
| **M3: DSH 编译 Skill 调通** | D5~D6 | `.agents/skills/2api-transpiler` | 输入第三方开源 2api 仓库，Agent 能够自主跑通 Pass 1/2/3 并生成可用包 |
| **M4: 闭环验证与一键热加载** | D7 | 端到端自动化集成 | 输入 GitHub URL，系统全自动完成逆向转译、沙箱门禁、写入 `cordis.patch.yml` 并热生效 |

---

## 8. 实战技术专题与演进支撑规范

为确保 Agent 编译器在转译野生开源项目时拥有确定的工程抓手与底线防御，本规范绑定以下两套专项技术白皮书：

1. **GitHub 热门 2API 逆向图谱与统一技术方案**：
   - 详见 [`TECHNICAL_SOLUTION_WILD_2API.md`](file://<LEGACY_WORKSPACE>/TECHNICAL_SOLUTION_WILD_2API.md)；
   - 深入剖析了 GitHub 上 10 款最流行 2API 项目（覆盖 Go、Python、TypeScript、Rust），确立了“五层解剖模型”与四种标准原型（OAuth 流、Cookie 伪装流、动态签名流、多协议聚合流）的轻量化收敛算法。
2. **架构审计与三大工程不变量规范**：
   - 详见 [`ARCHITECTURE_AUDIT.md`](file://<LEGACY_WORKSPACE>/ARCHITECTURE_AUDIT.md)；
   - 严格确立**“零容器强加（Zero-Docker Invariant）”**、**“本地自足相对寻址（Local-First Relative Pathing）”**与**“宿主与插件正交解耦（Host-Plugin Orthogonality）”**三大公理，杜绝企业级容器虚假实体与硬编码路径对个人自用系统的侵蚀。

---

## 🧭 架构文档导航
| 上一篇 | 📑 全景总索引 | 下一篇 |
|:---|:---:|---:|
| 📋 [产品需求文档 (PRD.md)](PRD.md) | 📑 [官方全景文档总索引 (DOCS_INDEX.md)](DOCS_INDEX.md) | ⚡ [2API Agent 编译器规范 (2API_AGENT_COMPILER_SPEC.md)](2API_AGENT_COMPILER_SPEC.md) ➡ |
