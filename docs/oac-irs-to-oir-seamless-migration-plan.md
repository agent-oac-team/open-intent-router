# OAC 从 IRS 无感迁移到 OIR 方案记录

日期：2026-07-09

## 1. 背景

OIR 的记忆和知识库能力已经具备基础闭环，目标是让 OIR 替代 `intent_recon_sys`（下称 IRS）接入 OAC，同时不影响当前 OAC 运行，也不影响现有 IRS 知识库下游调用方。

本次迁移的关键词是“无感”：OAC 前端、OAC Go 代理、现有知识库调用方、后台中控意图管理页面不应在第一阶段感知 OIR 的新内部契约。迁移入口应优先控制在服务地址、兼容 API 和灰度开关层。

## 2. 当前有效事实

### 2.1 OAC 当前依赖 IRS 的接口形态

OAC 前端通过 `apps/client/src/lib/central-api.ts` 调用：

- `POST /api/central/route`
- `POST /api/central/events/navigation`
- `POST /api/central/events/agent`
- `POST /api/central/plans/{plan_id}/confirm`

OAC Go 后端在 `apps/server/main.go` 中将 `/api/central/*` 代理到 FastAPI：

- `/api/central/route` -> `/api/v1/central/route`
- `/api/central/events/*` -> `/api/v1/central/events/*`
- `/api/central/plans/*` -> `/api/v1/central/plans/*`
- `/api/central/agents/*` -> `/api/v1/agents/*`
- `/api/central/sessions/*` -> `/api/v1/sessions/*`
- `/api/central/knowledge/*` -> `/api/v1/knowledge/*`

OAC Go 后台中控意图管理固定调用 IRS 老接口：

- `GET /api/v1/admin/agent-registry`
- `POST /api/v1/admin/agent-registry`
- `PUT /api/v1/admin/agent-registry/{agent_id}`
- `PATCH /api/v1/admin/agent-registry/{agent_id}/enabled`
- `DELETE /api/v1/admin/agent-registry/{agent_id}`

OAC 知识库管理代理当前通过 `CENTRAL_API_BASE_URL` 调用：

- `/api/v1/admin/knowledge/files`
- `/api/v1/admin/knowledge/files/{asset_id}`
- `/api/v1/admin/knowledge/files/{asset_id}/chunks`
- `/api/v1/admin/knowledge/files/{asset_id}/retry`

IRS 知识库下游还依赖：

- `POST /api/v1/knowledge/search`
- `POST /api/v1/knowledge/grouped-search`
- `POST /api/v1/knowledge/read`
- `GET /api/v1/knowledge/assets`
- `GET /api/v1/knowledge/assets/{asset_id}`
- `GET /api/v1/knowledge/assets/{asset_id}/chunks`
- `GET /api/v1/knowledge/chunks/{chunk_id}`

### 2.2 IRS 和 OIR 契约差异

IRS 的中控路由入口是 OAC 专用契约：

- `POST /api/v1/central/route`
- 请求字段包括 `user_id`、`user_tags`、`user_query`、`current_agent_id`、`current_agent_session_id`、`central_chat_history`、`agent_chat_history`
- 响应字段是 OAC 已使用的 `RouterOutput`：
  - `route.status`
  - `route.action`
  - `route.agent_id`
  - `route.message`
  - `context.source`
  - `context.current_agent_id`
  - `context.relation`
  - `context.artifact_refs`
  - `plan`

OIR 的核心路由入口是通用契约：

- `POST /api/v1/route`
- 请求字段包括 `user`、`input`、`current_agent`
- 响应字段是 OIR `RouteResponse`：
  - `assistant_message`
  - `decision.action`
  - `decision.target_agent_id`
  - `decision.message`
  - `decision.reason`
  - `context`
  - `execution_policy`
  - `next_action`
  - `plan`
  - `invocation`

因此 OAC 不应第一阶段直接接 OIR native route；需要 OIR 提供 IRS/OAC compatibility facade。

### 2.3 Agent Registry 差异

IRS/OAC 老注册表字段：

- `agent_id`
- `name`
- `description`
- `bot_id`
- `route_path`
- `allowed_user_tags`
- `positive_keywords`
- `negative_keywords`
- `enabled`

OIR `AgentDefinition` 字段更通用：

- `agent_id`
- `name`
- `description`
- `type`
- `trigger`
- `access_policy`
- `invocation`
- `ui_handoff`
- `context`
- `metadata`

兼容映射建议：

| IRS/OAC 字段 | OIR 字段 |
| --- | --- |
| `bot_id` | `invocation.config.bot_id` 或 `metadata.oac.bot_id` |
| `route_path` | `ui_handoff.route` |
| `allowed_user_tags` | `access_policy` 或 `metadata.oac.allowed_user_tags`，兼容层按老规则过滤 |
| `positive_keywords` | `trigger.keywords` / `trigger.positive_examples` |
| `negative_keywords` | `trigger.negative_examples` |

### 2.4 Knowledge 和 Memory 数据边界

OIR 已明确以下数据域边界：

- OIR 使用独立 `oir` PostgreSQL database，不把 OIR 表建在 IRS/OAC 的 `oac` database 中。
- `oir_memory_vectors` 用于 OIR/mem0 记忆向量。
- `oac_knowledge_chunks` 是 IRS 现有知识向量 collection，迁移期保留。
- `oir_knowledge_vectors` 是 OIR 后续知识向量目标 collection。
- Milvus collection 是派生索引，不是 canonical storage。
- 知识资产、chunk、引用、权限、导入状态应以 PostgreSQL canonical metadata/chunks 为准。
- 如果 embedding model、dimension、chunking strategy、vector schema 或 filter semantics 不一致，必须从 canonical chunks 重新索引，不能复制旧向量冒充迁移。

## 3. 迁移目标

1. OAC 前端、AI Sidebar、Go Central Proxy 第一阶段不改代码。
2. 通过调整 `CENTRAL_API_BASE_URL` 或网关目标，将 OAC 中控流量灰度切到 OIR。
3. OIR 对外提供 IRS/OAC 兼容接口，内部再转换为 OIR native schema。
4. 现有 IRS 知识库下游接口保持路径、请求、响应兼容。
5. 知识库迁移支持 proxy、shadow、native 三种模式，允许逐步验证和回退。
6. OIR 失败时可以 fail-open 回 IRS，避免影响 OAC 当前运行。
7. 迁移过程保留审计、diff、provenance 和 mapping，确保可追踪、可回滚。

## 4. 后续 OIR 开发治理原则

### 4.1 最大遗漏

当前最容易低估的风险不是“怎么把 IRS 换成 OIR”，而是 OAC 会成为 OIR 的第一个强宿主，并在后续持续反向塑造 OIR。

如果没有机制约束，OIR 很容易被一个个“为了 OAC 先这样吧”的小需求慢慢变成 OAC 专用后端。因此后续原则应明确为：

```text
OAC 可以是 OIR 的一等宿主，
但不能变成 OIR 的隐形核心。
```

迁移方案要解决的不只是一次切换，而是 OIR 如何在长期服务 OAC 的同时保持项目通用性、兼容性和可复用边界。

### 4.2 Host Profile 机制

OAC 特殊需求不应直接进入 OIR core，应收敛到 Host Profile / Adapter / Compat Facade / Policy Plugin。

推荐分层：

```text
OAC Platform
  -> OAC Frontend / Go Proxy / Admin

OIR OAC Host Layer
  -> oac_compat_api
  -> oac_registry_mapper
  -> oac_policy_profile
  -> oac_knowledge_profile
  -> oac_ui_handoff_profile

OIR Extensions
  -> knowledge provider
  -> memory provider
  -> registry source
  -> invoker provider
  -> auth/user resolver
  -> telemetry/exporter

OIR Core
  -> AgentDefinition
  -> RouteRequest / RouteResponse
  -> Registry / Router / Invoker / Plan
  -> MemoryContext / KnowledgeContext
  -> generic policy contracts
```

后续 OIR core 不应出现以下 OAC 专名或业务判断：

- `OAC`
- `IRS`
- `运营版`
- `展业版`
- `Coze bot_id`
- `route_path`
- `Feishu`
- `content_production`
- 银行私行业务字段

如果必须保留，应放入：

- `metadata.oac.*`
- `host_profiles/oac/*`
- OAC compat mapper
- OAC policy plugin

### 4.3 通用能力提升规则

每个 OAC 需求进入 OIR 前都先判断：

```text
这是 OIR 通用平台能力，还是 OAC 宿主适配需求？
```

进入 core 的条件：

1. 不包含 OAC 专有名词。
2. 其他宿主也可能需要。
3. 可以抽象成稳定接口。
4. 不破坏 OIR native contract。
5. 可以被独立测试，不依赖 OAC 数据或页面。

示例：

| 需求 | 处理方式 |
| --- | --- |
| `route_path` | 不进 core，映射到 `ui_handoff.route` |
| `bot_id` | 不进 core，放到 `invocation.config` 或 OAC metadata |
| `运营版/展业版` | 不进 core，映射到 roles/groups/attributes |
| `ui_handoff.route` | 可作为通用 core 能力 |
| `knowledge_context` | 可作为通用 core 能力 |
| `content_production asset_group` | OAC knowledge profile |

判断不清时，先放 adapter。等第二个宿主也需要时，再提升为 core contract。

### 4.4 控制面事实源

迁移期间最危险的不是 API 不兼容，而是控制面 split-brain。

必须明确以下事实源：

| 控制面对象 | 迁移期事实源建议 | 长期事实源建议 |
| --- | --- | --- |
| Agent 定义 | OIR AgentDefinition，OAC 老注册表通过 compat 映射 | OIR |
| OAC `bot_id` / `route_path` | OAC profile metadata | OIR host profile 或 OAC 管理后台同步到 OIR |
| 权限标签 | OAC profile mapper | OIR generic policy + OAC mapper |
| 知识 asset/chunk | IRS canonical 快照 + OIR migration manifest | OIR canonical metadata |
| 向量索引 | IRS/OIR 双 collection | OIR `oir_knowledge_vectors` |
| memory | 初期 recall-only，不做权威写入 | OIR memory ledger |
| events/plans | 切流前 IRS，切流后 OIR；避免双写无主 | OIR |

如果 OAC 后台、IRS、OIR 都能改同一类对象，必须指定主写方、只读方和同步方向。没有事实源设计时，不允许进入 native cutover。

### 4.5 行为兼容和版本承诺

无感迁移不是路径不变，而是行为不变。OAC 用户实际感知的是：

- 什么时候直接回复。
- 什么时候澄清。
- 什么时候打开页面。
- 什么时候继续当前智能体。
- 多步骤计划怎么展示和确认。
- 权限不足怎么提示。
- 知识证据怎么引用。
- 子智能体失败后怎么重试。
- OIR 异常时是否自动回退。

因此 OAC compat API 应像公开 API 一样管理：

- 有 contract tests。
- 有兼容矩阵。
- 有 deprecation 周期。
- 有变更日志。
- 有 capability negotiation。

OIR 可以通过 runtime capability 暴露：

- `oac_compat`
- `irs_fallback`
- `knowledge_grouped_search`
- `knowledge_native`
- `memory_context`
- `memory_write`
- `agent_registry_compat`

OAC 不应靠猜测判断 OIR 支持什么，应读取 capability 或由部署配置明确启用。

### 4.6 双重成功标准

OIR 替代 IRS 后，成功标准必须双重化：

| 维度 | 成功标准 |
| --- | --- |
| OIR core | 通用、干净、可扩展、无 OAC 专名污染 |
| OAC host | 稳定、兼容、业务可用、行为无感 |

两者冲突时，用 Host Profile / Adapter 隔离，而不是让其中一方直接吞掉另一方。

## 5. 推荐总体架构

```text
OAC Frontend
  -> /api/central/* 不变
  -> OAC Go Proxy 不变
  -> CENTRAL_API_BASE_URL 指向 OIR Compat Facade
       -> OIR Native Router / Registry / Memory / Knowledge
       -> 必要时 fallback/proxy 到 IRS
```

兼容层是第一阶段的核心，不应让 OAC 直接理解 OIR native contract。

## 6. OIR 需要新增的兼容门面

### 6.1 Central 兼容接口

新增：

- `POST /api/v1/central/route`
- `POST /api/v1/central/events/navigation`
- `POST /api/v1/central/events/agent`
- `POST /api/v1/central/plans/{plan_id}/confirm`

`/api/v1/central/route` 行为：

1. 接收 IRS `CentralRouteRequest`。
2. 转换为 OIR `RouteRequest`。
3. 调用 OIR native router。
4. 将 OIR `RouteResponse` 转回 OAC 已使用的 `RouterOutput`。
5. 保持 `route.action`、`route.agent_id`、`route.message`、`context.relation`、`plan` 旧语义。

字段转换要点：

| OAC/IRS 输入 | OIR 输入 |
| --- | --- |
| `user_id` | `user.id` |
| `user_tags` | `user.groups` 或 `user.attributes.oac_user_tags` |
| `user_query` | `input.text` |
| `source` | `source`，必要时做枚举兼容 |
| `current_agent_id` | `current_agent.agent_id` |
| `current_agent_session_id` | `current_agent.agent_session_id` |
| `central_chat_history` | OIR session/context metadata |
| `agent_chat_history` | OIR session/context metadata |

响应转换要点：

| OIR 输出 | OAC/IRS 输出 |
| --- | --- |
| `decision.status` | `route.status` |
| `decision.action` | `route.action` |
| `decision.target_agent_id` | `route.agent_id` |
| `assistant_message` 或 `decision.message` | `route.message` |
| `context.current_agent_id` | `context.current_agent_id` |
| `context.relation` | `context.relation` |
| `context.artifact_refs` | `context.artifact_refs` |
| `plan` | 旧 `Plan` 形状 |

### 6.2 Agent Registry 兼容接口

新增：

- `GET /api/v1/admin/agent-registry`
- `POST /api/v1/admin/agent-registry`
- `PUT /api/v1/admin/agent-registry/{agent_id}`
- `PATCH /api/v1/admin/agent-registry/{agent_id}/enabled`
- `DELETE /api/v1/admin/agent-registry/{agent_id}`

鉴权兼容：

- 支持 OAC Go 当前传入的 `X-Admin-Sync-Token`。
- 内部可映射到 OIR `X-Admin-Token` 或独立 compat token。

响应必须保持 OAC `CentralIntent` 结构，避免后台管理页面断裂。

### 6.3 Knowledge 兼容接口

新增或补齐 IRS 知识库接口：

- `POST /api/v1/knowledge/search`
- `POST /api/v1/knowledge/grouped-search`
- `POST /api/v1/knowledge/read`
- `GET /api/v1/knowledge/assets`
- `GET /api/v1/knowledge/assets/{asset_id}`
- `GET /api/v1/knowledge/assets/{asset_id}/chunks`
- `GET /api/v1/knowledge/chunks/{chunk_id}`
- `/api/v1/admin/knowledge/files*`

知识兼容接口支持三种运行模式：

| 模式 | 行为 |
| --- | --- |
| `proxy` | 直接转发到 IRS，保证下游无感 |
| `shadow` | 主结果使用 IRS，同时旁路调用 OIR native 并记录 diff |
| `native` | 主结果使用 OIR，失败时按开关 fallback 到 IRS |

## 7. 知识库迁移设计

### 7.1 不迁移向量，迁移 canonical 数据并重建索引

不要直接复制 IRS Milvus `oac_knowledge_chunks` 到 OIR `oir_knowledge_vectors`。

推荐流程：

```text
IRS canonical knowledge_assets / knowledge_chunks
  -> 导出快照
  -> 写入 OIR knowledge_sources / knowledge_chunks
  -> 使用 OIR embedding 配置重新索引到 oir_knowledge_vectors
  -> 运行 shadow search/grouped-search/read diff
  -> 达标后切换 native
```

### 7.2 迁移 manifest

建议为每个 asset/chunk/index 记录迁移 manifest：

| 字段 | 说明 |
| --- | --- |
| `old_asset_id` | IRS asset ID |
| `old_chunk_id` | IRS chunk ID |
| `old_index_id` | IRS index/vector ID |
| `old_collection` | 通常为 `oac_knowledge_chunks` |
| `new_source_id` | OIR source ID |
| `new_chunk_id` | OIR chunk ID |
| `new_index_id` | OIR index/vector ID |
| `new_collection` | 通常为 `oir_knowledge_vectors` |
| `content_hash` | 内容一致性校验 |
| `embedding_model` | embedding 模型 |
| `embedding_dimension` | embedding 维度 |
| `chunking_strategy` | 分块策略版本 |
| `vector_schema_version` | 向量 schema 版本 |
| `migration_status` | `pending` / `reindexed` / `skipped` / `retired` / `failed` |
| `diff_status` | shadow 对比结果 |

### 7.3 权限和来源要求

OIR native 知识检索必须保持：

- `allowed_user_tags` 过滤不放宽。
- `secret` 不进入检索结果。
- `disabled`、`deleted`、`failed` 资产不作为有效 evidence。
- evidence 保留 asset/source/chunk/citation/provenance。
- 双 collection 并行时，结果必须标注 collection provenance，不能混成无来源结果。

### 7.4 Golden Queries

知识库迁移缺的不是技术能力，而是可证明业务语义没有偏移的验收语料。

必须建立 golden queries，至少覆盖：

- 业务词召回。
- 活动表召回。
- 权益表召回。
- 企微模板召回。
- 产品/场景/KPI 组合召回。
- 权限过滤。
- 无命中。
- 低置信度。
- `restricted` / `secret` / `disabled` / `deleted` / `failed` 资产过滤。
- grouped-search 指定 asset group / asset keys。
- read/assets/chunks 精确读取。

没有 golden queries 时，不允许声称 OIR 知识库 native 行为等价 IRS。

## 8. 迁移阶段计划

### 阶段 0：契约抓取和 golden tests

目标：冻结当前 IRS/OAC 对外行为。

工作：

1. 抓取 `/api/central/route` 的典型请求响应。
2. 抓取 `/api/v1/admin/agent-registry` 的 CRUD 请求响应。
3. 抓取知识库 `search/grouped-search/read/assets/admin files` 的典型请求响应。
4. 建立 golden contract tests。
5. 建立知识库 golden queries。
6. 记录当前延迟、错误率、命中率和权限过滤表现。

退出标准：

- 有一组可重复运行的兼容测试。
- 覆盖中控新任务、继续当前 agent、退出 agent、多步骤 plan、agent event、知识检索、知识管理上传/查看/删除。
- 覆盖业务知识召回、权限过滤和无命中场景的 golden queries。

### 阶段 1：OIR Compat Facade

目标：OIR 能用老接口响应 OAC。

工作：

1. 新增 `/api/v1/central/*` compat API。
2. 新增 `/api/v1/admin/agent-registry` compat API。
3. 新增知识库 compat/proxy API。
4. 完成 IRS/OIR schema mapper。
5. 增加 compat 层单元测试和 contract tests。

退出标准：

- 不改 OAC 前端和 Go 代理，只把 `CENTRAL_API_BASE_URL` 指向 OIR，本地可跑通主链路。
- compat tests 全部通过。

### 阶段 2：Shadow Run

目标：真实流量下验证 OIR 决策和知识结果，不产生用户可见影响。

工作：

1. 主链路继续使用 IRS。
2. OIR 接收旁路请求，运行 route/knowledge shadow。
3. 记录 route decision diff：
   - action 是否一致
   - agent_id 是否一致
   - message 类型是否一致
   - plan 结构是否一致
4. 记录 knowledge diff：
   - matched 是否一致
   - evidence asset/chunk 命中差异
   - top_k 排序差异
   - 权限过滤差异
5. 记录 latency 和错误。

退出标准：

- 核心 agent 路由差异在可接受范围内。
- 知识权限无放宽。
- OIR p95 延迟满足 OAC 体验要求。
- 所有差异有 trace 可排查。

### 阶段 3：灰度切 Central

目标：小范围让 OAC 中控 route 走 OIR compat。

工作：

1. 对 canary 用户或测试环境切 `CENTRAL_API_BASE_URL` 到 OIR。
2. 知识库接口仍可保持 `proxy` 或 `shadow`。
3. 开启 `fail_open_to_irs`。
4. 观察中控侧栏、聊天类 agent、非聊天页面跳转、多步骤 plan、事件回流。

退出标准：

- canary 用户无明显感知。
- OIR 失败时可自动 fallback IRS。
- OAC Go in-flight guard、鉴权、注册表读取均正常。

### 阶段 4：知识库 Native 切换

目标：知识库下游逐步使用 OIR native 结果。

工作：

1. 完成 IRS canonical 数据导入 OIR。
2. 完成 `oir_knowledge_vectors` 重新索引。
3. `grouped-search/read/assets/admin files` native 实现对齐 IRS。
4. 将知识模式从 `proxy` 切到 `shadow`，再切到 `native`。
5. 对低风险 asset group 先灰度。

退出标准：

- 关键业务知识资产均完成 reindex。
- shadow diff 达标。
- 下游接口请求/响应兼容。
- 可按 asset group 或用户回退。

### 阶段 5：IRS 冻结和下线

目标：OIR 成为 OAC 中控和知识库主服务。

工作：

1. 停止 IRS 新写入或只读冻结。
2. 确认没有下游直接访问 IRS。
3. 保留 OIR compat API 至少一个版本周期。
4. 归档 IRS 数据、迁移 manifest 和 diff 报告。
5. 下线 IRS 服务或转为冷备。

退出标准：

- 监控确认无 IRS 活跃流量。
- 回滚预案保留。
- OIR native 和 compat 均有测试覆盖。

## 9. 推荐配置开关

```env
OIR_OAC_COMPAT_ENABLED=true
OIR_COMPAT_KNOWLEDGE_MODE=proxy
OIR_IRS_FALLBACK_BASE_URL=http://127.0.0.1:8000
OIR_SHADOW_COMPARE_ENABLED=true
OIR_FAIL_OPEN_TO_IRS=true
OIR_MIGRATION_CANARY_USERS=
OIR_HOST_PROFILE=oac
OIR_CONTROL_PLANE_SOURCE=oir

OIR_MEMORY_MODE=recall_only
OIR_KNOWLEDGE_NATIVE_ENABLED=false
OIR_KNOWLEDGE_SHADOW_SAMPLE_RATE=1.0
```

建议含义：

| 配置 | 说明 |
| --- | --- |
| `OIR_OAC_COMPAT_ENABLED` | 是否启用 OAC/IRS 兼容接口 |
| `OIR_COMPAT_KNOWLEDGE_MODE` | 知识接口运行模式：`proxy` / `shadow` / `native` |
| `OIR_IRS_FALLBACK_BASE_URL` | IRS fallback 地址 |
| `OIR_SHADOW_COMPARE_ENABLED` | 是否记录 OIR/IRS diff |
| `OIR_FAIL_OPEN_TO_IRS` | OIR 异常时是否回退 IRS |
| `OIR_MIGRATION_CANARY_USERS` | 灰度用户列表 |
| `OIR_HOST_PROFILE` | 当前宿主 profile，例如 `oac` |
| `OIR_CONTROL_PLANE_SOURCE` | 控制面事实源，例如 `oir` / `irs` / `proxy` |
| `OIR_MEMORY_MODE` | 记忆能力模式：`off` / `recall_only` / `write_pending` / `write_enabled` |

## 10. 验收门禁

### 10.1 中控兼容门禁

- OAC 前端无需改代码。
- OAC Go proxy 无需改路径映射。
- `/api/v1/central/route` 老响应结构完全兼容。
- `route.action` 行为与 OAC `AISidebar` 当前分支兼容。
- plan confirm 老接口可用。
- navigation event 和 agent event 可写入并推动后续路由。
- 行为兼容覆盖 reply、clarify、open_agent、continue_agent、exit_agent、show_plan、unsupported、silent。
- OIR capability 明确声明是否支持 `oac_compat` 和 `irs_fallback`。

### 10.2 注册表兼容门禁

- OAC `/api/central-agent-registry` 可正常读取。
- OAC 后台中控意图管理可正常增删改查。
- `bot_id` 和 `route_path` 互斥规则保持。
- `allowed_user_tags` 老权限语义保持。
- 控制面事实源唯一，不存在 OAC 后台、IRS、OIR 多方同时主写同一 agent 的情况。

### 10.3 知识库兼容门禁

- `search/grouped-search/read/assets/admin files` 请求响应兼容。
- 权限过滤不放宽。
- `secret/deleted/disabled/failed` 资产不进入有效结果。
- evidence 保留 citation/provenance。
- OIR native 检索失败可回退 IRS。
- golden queries 通过。
- shadow diff 达标后才允许切 native。

### 10.4 数据和运维门禁

- OIR 使用独立 `oir` database。
- memory、knowledge collection 分离。
- migration manifest 可追踪每个 asset/chunk/index。
- shadow diff 和错误有可观测页面或日志。
- 有一键回滚到 IRS 的明确操作。
- 回滚策略覆盖 OIR 已写入的 memory、events、plans、knowledge migration manifest。
- OIR core 检查无 OAC 专名污染。

## 11. 风险和应对

| 风险 | 应对 |
| --- | --- |
| OIR native contract 与 OAC 老 contract 不一致 | 用 compat facade 隔离，先跑 golden tests |
| OAC 后台注册表断裂 | OIR 补 `/api/v1/admin/agent-registry` 老接口 |
| 知识库下游直接依赖 IRS 响应字段 | 知识接口先 proxy，再 shadow，再 native |
| 向量复制导致语义/维度/过滤不一致 | 只从 canonical chunks reindex，不复制向量 |
| OIR 生产配置失败影响 OAC | `fail_open_to_irs=true`，保持 IRS fallback |
| 记忆自动写入不受控 | 迁移初期 `recall_only` 或 `write_pending`，不直接启用全量写入 |
| 权限过滤放宽 | 权限 diff 作为 P0 门禁，任何放宽都阻塞切流 |
| OAC 长期反向污染 OIR core | 建立 Host Profile / Adapter / Compat Facade，core 禁止 OAC 专名 |
| 控制面 split-brain | 明确 Agent、知识、权限、memory、events/plans 的事实源和同步方向 |
| 回滚只切 URL 但状态已写入 OIR | 初期限制写入，回滚 runbook 覆盖 memory/events/plans/manifest 处理 |
| OIR 检索看起来可用但业务语义偏移 | 建立知识库 golden queries 和 shadow diff 门禁 |
| compat API 无版本承诺 | 把 OAC compat 当公开 API 管理，补 contract tests、能力声明和 deprecation 周期 |

## 12. 首批开发任务建议

1. 建立 `replace-irs-with-oir-oac-compat-migration` OpenSpec change。
2. 建立 `HOST_PROFILE=oac` 的设计和配置边界。
3. 明确控制面事实源设计：
   - Agent registry
   - 权限标签
   - 知识资产
   - memory
   - events/plans
4. 增加 OAC/IRS compat schema：
   - `CentralRouteRequest`
   - `RouterOutput`
   - `AgentRegistryItem`
   - IRS knowledge request/response schemas
5. 实现 `CentralCompatService`：
   - IRS request -> OIR request
   - OIR response -> IRS response
6. 实现 `AgentRegistryCompatService`。
7. 实现 `KnowledgeCompatProxyService`，第一期只需 proxy + shadow hook。
8. 增加 OAC compat contract tests。
9. 增加知识库 golden queries。
10. 增加 shadow diff log model 或文件日志。
11. 增加 runtime capability 输出。
12. 增加 migration config 和 runtime config 可观测字段。
13. 写一份切流和回滚 runbook：
   - 启用 OIR compat
   - 切 `CENTRAL_API_BASE_URL`
   - 观察指标
   - 回滚到 IRS
   - 处理 OIR 已写入状态

## 13. 待确认问题

1. 现有 IRS 知识库下游除了 OAC 前端/Go 代理外，是否还有 Coze、脚本、外部工作流直接访问。
2. 知识库 canonical 数据迁移从 IRS PostgreSQL 直接导出，还是通过 HTTP read/assets 接口导出。
3. `allowed_user_tags` 在 OIR 中长期应映射为 roles、groups、attributes，还是保留 OAC compat metadata。
4. OIR route shadow 的差异阈值如何定义：必须 agent_id 一致，还是 action 类型一致即可。
5. 记忆能力在替代 IRS 的首个版本中是否只开启 recall，不开启自动 write。
6. OAC 生产是否允许按用户、租户或环境变量灰度 `CENTRAL_API_BASE_URL`。
7. OAC compat API 的版本周期、弃用周期和兼容矩阵如何维护。
8. 切流后 Agent registry、知识资产、events/plans 的主写方分别是谁。
9. 哪些 OAC 特殊字段必须长期保留为 profile metadata，哪些可以提升为通用 OIR contract。

## 14. 推荐结论

第一阶段不要把 OAC 改造成 OIR native client。正确路径是：

```text
先让 OIR 模仿 IRS 的对外形状，
再让 OIR 内部逐步替代 IRS 的实现，
最后再考虑 OAC 是否要升级到 OIR native contract。
```

同时，后续 OIR 开发必须先建立 Host Profile、OAC compat contract tests 和控制面事实源设计。否则 OIR 很可能不是被一次性迁移污染，而是在后续 OAC 特殊需求中被慢慢吞掉。

这样可以最大程度保证 OAC 当前运行不受影响，也能保护已有 IRS 知识库下游，实现真正的无感迁移。
