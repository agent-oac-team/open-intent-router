# App-Desc：OIR 应用地图

本文描述当前代码的模块、运行入口、依赖方向、事实源和治理区域。它是导航图，不重复 API 字段和具体业务算法。

## 一句话定位

OIR 是一个可插拔的意图路由、Agent 编排和受治理 Context / Memory / Knowledge 后端；OAC 通过同仓的 Host Adapter 使用 IRS 兼容契约，OIR Core 保持宿主无关。

## 运行入口

| 入口 | 命令 | API 形态 |
| --- | --- | --- |
| OIR Native Runtime | `uvicorn app.main:app --reload` | `/health`、`/ready`、`/api/v1/**` |
| OAC Host Runtime | `uvicorn host_apps.oac.main:app --reload` | IRS Legacy `/api/v1/**`；OIR Native 挂载于 `/oir/api/v1/**`；另有 `/capabilities` |
| 本地测试 UI | `cd web && npm run dev` | 默认通过 Vite 代理调用 Native Runtime |

## 依赖方向

```text
host_apps/oac
  -> host_adapters/oac
    -> app/application/ports.py
      -> app services / repositories / schemas

app  -X->  host_adapters/oac
```

Host Adapter 可以包含 OAC、IRS、Coze、Legacy 字段和兼容行为；Core 的公共 Schema、Service、Prompt、数据库模型和默认配置不得反向吸收这些概念。

## 模块地图

| 路径 | 职责 | 修改时重点 |
| --- | --- | --- |
| `app/api` | Native FastAPI 协议适配、依赖注入、错误转换 | 不堆业务逻辑；同步 Schema、Service、测试和 API 文档 |
| `app/schemas` | Native 请求、响应和领域契约 | 兼容性、校验边界、敏感投影 |
| `app/application` | Host 可依赖的通用应用端口 | 不出现宿主专有类型；端口变化需要 Adapter contract tests |
| `app/services` | Router、Invocation、Plan、Turn、Context、Memory、Knowledge 业务编排 | 保持领域边界；复用现有生命周期与事务入口 |
| `app/repositories` | memory / database / file 存储与事务实现 | canonical、幂等、所有权、并发和序列化一致性 |
| `app/db`、`sql` | SQLAlchemy 模型和 PostgreSQL 初始化 / 迁移快照 | 数据表与约束是事实源；SQLite / PostgreSQL 行为对齐 |
| `app/llm`、`app/invokers`、`app/plugins` | LLM、Agent 调用与 Evidence 扩展 | Provider 细节不泄漏到公共契约；超时与失败可控 |
| `host_adapters/oac` | IRS 兼容 API、Mapper、V2 identity、Fallback、Shadow、Cutover | 只调用应用端口；current-only V2；写操作 fail closed |
| `host_apps/oac` | OAC 进程组合、Host 配置和运行门禁 | Composition only；启动前校验数据域、key、write fence |
| `web/src` | 本地开发测试台 | 非生产 UI；每轮数据与真实后端 trace 对齐 |
| `tests` | Native、Adapter、契约、数据库和前端外的回归测试 | 负向安全矩阵、失败注入、隔离与 fixture 稳定性 |
| `scripts` | 迁移、验证、replay、reindex、smoke 和 repair 工具 | 默认安全、显式 apply、幂等、输出脱敏报告 |
| `openspec` | 当前和历史变更的需求、设计、规格与任务 | 行为状态以 change 的 `tasks.md` 和验收证据为准 |

## 代码治理区域

| 区域 | 路径 | 规则 |
| --- | --- | --- |
| 通用 Core 区 | `app/**` | 只接受跨宿主通用概念；按 Native 契约和 Core 分层修改 |
| Host 防腐区 | `host_adapters/oac/**` | 允许 Legacy / OAC 语义，但必须隔离在应用端口之外 |
| Composition 区 | `host_apps/oac/**` | 只组装 Core 与 Adapter、配置和中间件，不复制业务状态机 |
| 冻结兼容区 | `tests/contract/oac_irs/**` | 代表 IRS 契约基线；更新必须有版本化决策和 replay 证据 |
| 迁移与证据区 | `docs/App-Research/evidence/**`、`migration-input/**`、相关 `scripts/**` | 记录过渡过程；不能反向定义 Core 公共契约 |
| 示例与本地开发区 | `config/*.example.yaml`、`web/**` | 不代表生产 Agent、权限或 UI；示例不得进入 Core 默认业务逻辑 |
| 生成物区 | `web/dist`、`*.egg-info`、cache、`.data`、部分 `artifacts` | 不手工编辑，不作为代码评审的事实源 |

OIR 当前没有一块应继续扩展的“旧 Core”。IRS Legacy 行为被收敛为冻结契约和 Adapter；新增兼容逻辑不得散落回 `app/`。

## 领域事实源

| 领域 | Canonical 事实源 | 派生 / 展示层 |
| --- | --- | --- |
| Agent Registry | `agent_definitions`、`registry_revisions`；file backend 仅开发 / 兜底 | OAC Legacy Registry 投影、LLM Candidate、UI 列表 |
| Route / Plan / Run | Canonical Turn、Run、Result、Plan、Event 及事务约束 | Session message、Route Log、前端 ConversationTurn |
| Memory | PostgreSQL `memory_items`、`memory_revisions`、`memory_events`、formation / index operation | mem0 / Milvus vector、Memory Context、Debug Trace |
| Knowledge | PostgreSQL source / asset / chunk / import job / migration manifest | Milvus vector、citation、Legacy response projection |
| Host Identity | OAC Go 当前用户事实 + `OIR-HOST-V2` 受信 envelope | Body user / edition 只作一致性校验，不是授权证据 |
| 配置 | `Settings` / `OacHostSettings` 定义 | `.env.example` 是无敏感值的使用说明 |
| API | Pydantic Schema + FastAPI router + contract tests | Markdown 概览和运行时 OpenAPI |

## 关键链路

### Native Route / Invoke

```text
API -> RouterService -> Registry access filter -> Context Pipeline
    -> LLM / deterministic decision -> Canonical Turn
    -> optional Invocation / Plan -> Run + Result + Outbox
    -> asynchronous Memory Formation / Index
```

### OAC Legacy

```text
OAC Go / Coze -> OIR-HOST-V2 -> OAC Adapter
    -> schema / identity / operation policy
    -> OIR application ports
    -> Legacy response projection
```

## 当前工作状态

- `replace-irs-with-oir-oac-adapter` 仍有测试环境 replay、排空、cutover 和 IRS 下线证据未完成。
- `fix-admin-hmac-and-retire-host-v1` 的代码已收敛到 current-only V2，但测试环境 replay / 报告仍是归档门禁。
- 其他未归档 change 不能仅凭目录存在判断状态；逐项查看其 `tasks.md`。
- 当前工作树包含未提交的功能与文档改动。文档治理或其他任务不得重置、覆盖或顺手格式化这些改动。

## 当前契约与架构

### 契约

| 文档 | 主要用途 |
| --- | --- |
| [API 概览](contracts/api.md) | OIR Native API、路由、Plan、Memory、Knowledge 和 Session 契约 |
| [Agent 定义](contracts/agent-definition.md) | Agent Definition、访问控制、调用和 Context 配置 |
| [Legacy / Native 兼容矩阵](contracts/legacy-native-compat-matrix.md) | 旧接口、操作类型、应用端口和回退规则 |

### 架构与集成

| 文档 | 主要用途 |
| --- | --- |
| [Canonical Turn 数据模型](architecture/canonical-turn-data-model.md) | Turn、Outbox、事务不变量和滞留 Turn 对账 |
| [Evidence Provider](architecture/evidence-provider.md) | 路由证据与 Agent Knowledge Context 的边界 |
| [mem0 记忆闭环集成](architecture/mem0-memory-integration.md) | PostgreSQL / mem0 / Milvus 边界、配置和 smoke |
| [OAC Host Adapter](architecture/oac-host-adapter.md) | Core / Adapter 边界、V2 身份、Ticket、Fallback 和 Capability |
| [Registry 迁移与主写规则](architecture/registry-migration.md) | OAC Registry 映射、entitlement、审计和迁移命令 |

### 界面与展示

| 文档 | 主要用途 |
| --- | --- |
| [可视化测试 UI](interfaces/visual-test-ui.md) | 本地调试台能力和真实性边界 |
| [中控运行图](interfaces/routing-journey-visualization.md) | 每轮运行图的数据来源、状态和可访问性 |

## 深入阅读

- Context / Memory：[Context Pipeline 验收](../App-Research/validation/governed-context-pipeline-acceptance.md)、[Formation Runbook](../App-Adr/develop/skills/runbooks/conversation-memory-formation-rollout.md)
- OAC 迁移：[需求追踪矩阵](../App-Research/tracking/requirements-traceability.md)、[Host 迁移 Runbook](../App-Adr/develop/skills/runbooks/oac-host-migration.md)
- 设计取舍与历史证据：[App-Research](../App-Research/README.md)
- 全量文档：[文档中心](../README.md)
