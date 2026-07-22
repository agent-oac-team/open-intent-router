# API 概览

> OAC 使用的 IRS 兼容 API 由独立 Host Adapter 暴露，不属于 OIR Native API。
> 完整路径、身份、Ticket 和 capability 契约见 [OAC Host Adapter](../architecture/oac-host-adapter.md)。

本文档说明 `open-intent-router` MVP 阶段提供的主要接口边界。接口以 FastAPI 暴露，完整字段定义以代码中的 Pydantic Schema 和运行时 OpenAPI 文档为准。

## 健康检查

- `GET /health`：服务进程健康检查。
- `GET /ready`：服务就绪检查，会校验关键依赖是否可用。

## 路由

### `POST /api/v1/route`

只执行意图识别和路由决策，不调用目标 Agent。

每个被接受的语义请求会在路由开始时按可信
`tenant_id/user_id/request_id` 创建或恢复一个 Canonical Turn。相同身份、session、source
和当前输入的重试返回同一逻辑 Turn；跨身份或输入冲突会被拒绝。Turn 只保存当前受控输入，
不会复制 `frontend_context`、Host 历史消息、页面状态或 Provider 会话 ID。

- `reply/clarify/unsupported/silent` 且没有 Plan/Invocation 时，Router 直接完成 Turn，不创建伪 Run。
- 返回待确认 Plan 时，Turn 绑定 `plan_id` 并保持 `blocked`，等待后续可信状态推进。
- 外部 Agent 路径保持活动状态，只有所有权一致的最终 Result 才能完成 Turn。
- Turn/Run/Result/Plan/Outbox 的最终收口使用单一数据库事务；Memory Provider 不在主事务内调用。

Canonical Turn 当前是 OIR 内部应用契约，不新增公开 Turn HTTP 端点。数据库结构和状态不变量见
[canonical-turn-data-model.md](../architecture/canonical-turn-data-model.md)。

典型返回内容包括：

- 顶层 `assistant_message`，即普通聊天窗口应展示给用户的主文案。
- 候选 Agent 列表。
- 最终选中的 `target_agent_id`。
- 路由动作，例如 `reply`、`clarify`、`open_agent`、`continue_agent`、`exit_agent`、`show_plan`、`unsupported` 或 `silent`。
- 置信度、理由、缺失输入、Evidence 命中信息。
- 路由日志 ID，便于后续审计。

消息字段职责：

- `assistant_message`：聊天窗口主来源，后端会在路由归一化阶段校验和补齐。
- `decision.message`：路由阶段兼容文案，旧客户端可回退使用。
- `decision.reason`：路由解释和调试原因，不作为聊天主文案。
- `next_action.message`：Plan 或宿主协作状态提示，例如确认计划、补充输入或等待事件。
- `AgentInvocationResult.message`：Agent 调用摘要，只属于调用结果。

路由前筛选顺序固定为：权限过滤 > 强确定性规则 > 语义/标签筛选 > LLM 判断。

- 权限过滤是硬边界。候选 Agent 会先按 enabled、角色、用户组、租户和属性过滤；后续 Evidence、固定问、标签或 LLM 都不能扩大到用户不可访问的 Agent。
- 固定问强命中是强路由。当 Evidence Provider 返回可用 Agent 的强 `route_override` 时，本轮直接返回路由结果，不再调用 LLM。
- 固定问强命中但目标 Agent 对当前用户不可用时，返回 `status=unsupported`、`action=unsupported` 和无权限提示，并在 `context.metadata.permission_denied=true` 中记录原因。
- 标签/语义筛选在当前版本只作为召回观察信号，不裁剪候选集。命中信息会写入 `context.metadata.tag_filter`、`tag_filter_matched_agent_ids` 和 `tag_filter_matches`；传给 Evidence Provider 和 LLM 的候选集仍是权限过滤后的全部可用 Agent。

路由与 Agent 执行现在使用统一的 governed Context Pipeline。内部对象严格分层：

- `ContextCandidate`：Provider 返回的未治理候选，只存在于单次 assembly。
- `ContextPack`：完成 purpose/consumer、权限、时效、脱敏、去重、冲突和预算处理后，某个消费者实际可用的 included items。
- `ContextProjection`：Router 或目标 Agent 的最终白名单输入。Router Prompt 在 `enforced` 模式只消费 Projection，不再直接序列化完整 `RouteRequest`、`RouteContext.metadata`、Debug Pack 或 dropped candidates。
- `ContextTrace`：记录 Provider outcome、authority、visibility、去重/冲突、预算、版本和 Projection hash；Trace 不属于模型输入。

`RouteContext.metadata.context_pack` 保留为兼容 Debug 摘要，包含：

- `budget`：本轮上下文 token 预算、可选来源预算、单项限制和字符/token 换算比例。
- `usage`：已用 token、估算来源、保留/丢弃/截断数量、来源分布和丢弃原因。
- `selection`：每个候选 Context Item 的 `item_id`、`source`、`scope`、`role`、优先级、估算 token、是否入选、裁剪/丢弃原因和 Agent 会话标识。
- `items`：实际 included items 的有界内容预览，不应作为长期持久化事实源。
- `purpose`、`consumer`、`trace_id` 和 policy/budget/projection version。
- `provider_outcomes`、治理 decisions 和 bounded Projection hash/usage。

`legacy`/`observe` 兼容期内，旧 metadata 可能继续存在；`enforced` 会移除这些原始副本，只保留数量或 key 摘要。Route Log 只持久化 bounded Pack/Trace usage、Provider 状态、selection 和 Projection hash，不保存完整 Prompt、无界历史正文或完整 structured values。

Context Pack 默认预算可通过 `.env` 配置，修改后需要重启后端：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONTEXT_DEFAULT_TOKEN_BUDGET` | `2000` | 默认总 token 预算 |
| `CONTEXT_MAX_TOKEN_BUDGET` | `8000` | 请求级预算覆盖的最大安全上限 |
| `CONTEXT_DEFAULT_SOURCE_BUDGETS` | 空 | 来源预算，格式如 `evidence:600,agent_history:500` |
| `CONTEXT_CHARS_PER_TOKEN` | `4` | 字符数换算 token 的估算比例 |
| `CONTEXT_PER_ITEM_TOKEN_LIMIT` | `512` | 单个 Context Item 的 token 上限 |
| `CONTEXT_PER_ITEM_CHAR_LIMIT` | `2000` | 单个 Context Item 的字符上限 |
| `CONTEXT_ALLOW_REQUEST_BUDGET_OVERRIDE` | `true` | 是否允许请求或 `frontend_context` 覆盖预算 |
| `CONTEXT_ALLOW_SUMMARY_PLACEHOLDER` | `true` | 截断时是否记录摘要占位标记 |
| `CONTEXT_PIPELINE_MODE` | `legacy` | `legacy`、`observe` 或 `enforced` |
| `MEMORY_MODE` | `off` | 唯一记忆行为模式：`off`、`observe` 或 `on` |
| `CONTEXT_ROUTE_KNOWLEDGE_ENABLED` | `false` | 是否启用 route-stage Knowledge Provider |
| `CONTEXT_ROUTE_KNOWLEDGE_SOURCE_IDS` | 空 | Router 可请求的 Knowledge source IDs |
| `CONTEXT_POLICY_VERSION` | `context-policy-v1` | 治理策略版本 |
| `CONTEXT_BUDGET_VERSION` | `context-budget-v1` | 预算策略版本 |
| `CONTEXT_PROJECTION_VERSION` | `context-projection-v1` | Projection 契约版本 |

Rollout 语义：

- `legacy`：保留旧 Router Prompt 输入；但 `MEMORY_MODE=on` 的 Memory 路径仍强制使用 Governed Context，不能借此关闭记忆治理。
- `observe`：构建新 Pack/Projection/Trace，记录 `legacy_input_hash` 和 `projection_hash`，Router LLM 仍只调用一次并使用旧输入。
- `enforced`：Router Prompt 和 Agent context 只由 governed Projection 生成。

M5/M6 起，路由器和 Invoker 会根据目标 Agent 的 `context` 配置组装平台治理上下文。目标 Agent 不直接自由调用记忆或知识检索，而是消费稳定字段：

- `memory_context`：包含 `summary`、结构化 `items`、`status`、`truncated`、`errors` 和调试 `metadata`。
- `knowledge_context`：包含 `summary`、结构化 `items`、`citations`、`source_ids`、`status`、`truncated`、`errors` 和调试 `metadata`。

当 `MEMORY_MODE=on` 且 `context.memory.mode=prefetch` 并显式声明非空 scopes 时，Agent execution Pack 才会按声明 scope 预召回记忆。全局 `on` 不向 Agent 继承 route defaults；当前输入只控制本轮执行，不会直接改写长期记忆。

Agent 知识预取默认关闭。只有 `context.knowledge.mode=prefetch` 时才会在调用前检索知识；`context.knowledge.mode=controlled_retrieval` 用于固定工作流节点按模板调用检索，不允许模型任意决定检索。Router 阶段 Memory 只在 `MEMORY_MODE=on` 时启用并固定 scopes 为 `user_preference,stable_fact`；Knowledge 仍由显式 route policy 控制。

同一个 Route/Invoke 流程使用 request-scoped assembly cache。Router 检索候选可被目标 Agent 复用，但 Agent 阶段必须重新应用 Agent visibility、声明 source/scope 和预算；不同请求、用户或租户之间不复用。

### `POST /api/v1/route-and-invoke`

先执行路由，再在目标 Agent 可调用且输入满足要求时执行调用。

适用场景：

- 宿主应用希望后端直接完成“识别意图 + 调用工具”。
- 目标 Agent 是 HTTP API、本地函数或 Mock Agent。
- 调用过程需要统一记录 Agent Run、事件和结果。

如果目标 Agent 只能由前端或宿主系统处理，例如 `ui_handoff`，接口会返回路由交接信息，而不会执行外部调用。

已有 Canonical Turn 的调用使用两个短事务收口：调用 Agent 前原子创建 Run 并把 Turn 置为
`running`；Agent 返回后原子更新 Run、写 Result、完成 Turn 并插入唯一 `turn.completed` Outbox。
外部 Agent 调用位于两个事务之间。任一启动写入失败时不会调用 Agent；任一终态写入失败时接口不会把
部分成功报告为成功。相同 owner/request 的终态重试复用原 Run/Result/Turn/Outbox。

`POST /api/v1/route-and-execute` 的非 Plan 单 Agent 分支复用同一收口路径。显式
`POST /api/v1/invoke` 仍保留无 Canonical Turn 的 direct-invoke 语义。

### `POST /api/v1/route-and-execute`

先执行路由，再根据执行策略处理结果：

- 单 Agent 请求：复用现有 invocation 路径。
- 多意图请求且 `execution_policy=auto_execute`：创建 Plan 后由后端 PlanExecutor 推进可执行步骤。
- 多意图请求且 `execution_policy=require_confirmation`：返回 Plan 和 `next_action.type=confirm_plan`，不立即执行。

多意图的主契约是 `plan` 字段。`decision.action=show_plan` 仅保留兼容语义，客户端不应只依赖该 action 判断是否展示计划。

## 显式调用

### `POST /api/v1/invoke`

根据 `agent_id` 显式调用目标 Agent，不再重新做意图识别。

MVP 支持的 Invoker：

- `mock`：返回配置好的 Mock 响应，适合本地开发和测试。
- `http`：调用配置的 HTTP Endpoint。
- `local_function`：调用受信任的本地注册函数。
- `ui_handoff`：返回宿主应用所需的路由交接数据，不执行外部系统调用。

如果请求输入中已经包含 `memory_context` 或 `knowledge_context`，Invoker 会沿用调用方提供的上下文字段；否则会按 Agent Definition 自动组装。

## Memory

Memory API 用于 M5 记忆召回、低风险写入候选处理、TTL 清理和调试检查。mem0 作为可选策略层隐藏在 adapter 后面；OIR 仍负责租户启用、scope、TTL、可见性、权限、审计和 Agent 可见范围。

- `POST /api/v1/memories/recall`：按 `query`、`user`、`scopes`、`subject`、`agent_id` 和 `max_items` 召回记忆，返回 `MemoryRecallResponse.context`。
- `POST /api/v1/memories/write-candidates`：提交候选记忆，服务根据置信度、敏感标记、scope TTL 等策略返回 accepted/rejected 决策。
- `POST /api/v1/memories/cleanup`：清理已过期记忆，并记录过期事件。
- `GET /api/v1/memories/debug`：按 user、tenant、agent、scope 或 request ID 查看当前可见记忆项、写入/过期事件、mem0 provider 状态、外部 ID 映射和最近错误摘要。按 request 查询时额外返回 `request_trace`，包含 `overall_stage/terminal/retryable/reason_code` 及 Turn、Run/Result、Outbox、Formation、Memory/Revision、Index 的有界 ID 关联。正文和 Provider 凭证不会进入该高层 trace。

`request_trace.overall_stage` 的主要值为 `turn_pending`、`turn_running`、`outbox_pending`、
`formation_skipped`、`formation_pending/retry/dead_letter`、`completed_no_candidate`、
`policy_rejected`、`memory_persisted_index_pending`、`index_retry/dead_letter`、`persisted` 和
`trace_missing`。`persisted` 仅表示 canonical Item/Revision active 且 index operation ready。

真实 mem0 记忆闭环使用 `MEMORY_STRATEGY_PROVIDER=mem0` 开启，本地 Milvus 统一使用 Milvus Lite，默认 memory collection 为 `oir_memory_vectors`。OIR 会把 mem0 add/search/delete history 和 `memory_id`/`mem0_memory_id` 映射写入 PostgreSQL-backed `memory_events`，不把 mem0 SDK 内部 SQLite history 当作长期事实来源。完整配置、失败策略、Mermaid 流程和 smoke 路径见 [mem0-memory-integration.md](../architecture/mem0-memory-integration.md)。

记忆 scope 包括：`user_preference`、`stable_fact`、`task_memory`、`artifact_reference`、`session_summary`。其中 `task_memory`、`artifact_reference`、`session_summary` 默认 14 天过期；用户偏好和稳定事实默认长期保留。

## Knowledge

Knowledge API 是 M6 的通用治理检索接口，不强绑定 Agent ID。调用方必须声明 `caller_type`、可选 `caller_id`、`purpose`、用户和租户上下文，KnowledgeService 会再次执行 source policy。

- `POST /api/v1/knowledge/search`：通用知识检索。支持 `caller_type=router|agent|host|admin`，`purpose=route_evidence|agent_execution|debug|preview`，以及 `source_ids`、`source_tags`、`top_k`。
- `GET /api/v1/knowledge/debug`：查看知识源、chunk 和检索日志，便于排查 denied source、timeout、provider error 和命中情况。

Agent Definition 中的 `context.knowledge.source_ids` 只是请求来源，不是最终授权证明。若知识源因角色、用户组、租户或启用状态被拒绝，结果会在 `denied_source_ids` 和 `knowledge_context.metadata.denied_source_ids` 中记录。检索失败或超时默认降级为 `status=error|timeout`，不阻塞普通调用。

知识向量过渡期保留 `oac_knowledge_chunks` 与 `oir_knowledge_vectors` 双 collection。Milvus collection 只是派生索引；embedding 模型、维度、chunk 策略或 schema 不兼容时必须基于 canonical chunks reindex，不能直接复制向量。检索和 debug/citation 应保留 collection provenance。

## Agent 查询

公开查询接口：

- `GET /api/v1/agents`
- `GET /api/v1/agents/{agent_id}`
- `POST /api/v1/agents/available`

公开接口会隐藏敏感配置，例如密钥、Header Token、私有调用参数等。宿主应用可通过 `available` 接口按用户角色、用户组、租户和属性过滤可用 Agent。

## Agent 管理

管理接口通常需要传入 Admin Token：

- Header：`X-Admin-Token: <token>`
- 或：`Authorization: Bearer <token>`

本地开发例外：当 `APP_ENV=local` 且未配置 `ADMIN_API_TOKEN` 时，后端只允许来自本机 loopback 的 Admin 写操作。非 local 环境必须配置 `ADMIN_API_TOKEN`；如果 local 环境也配置了 token，则仍然需要传入 token。

接口列表：

- `GET /api/v1/admin/agents`
- `POST /api/v1/admin/agents`
- `PUT /api/v1/admin/agents/{agent_id}`
- `PATCH /api/v1/admin/agents/{agent_id}/enabled`
- `DELETE /api/v1/admin/agents/{agent_id}`
- `POST /api/v1/admin/registry/reload`

说明：

- `database` 模式支持完整 CRUD。
- `file` 模式主要用于只读加载，不适合运行时变更。
- `hybrid` 模式以数据库为主，数据库不可用时才使用本地文件兜底。

## 事件

事件接口用于接收 Agent 执行过程中的状态变化、进度、日志和结果片段。

- `POST /api/v1/events/agent`
- `POST /api/v1/runs/{run_id}/events`

事件 ID 具备幂等语义。重复提交相同事件 ID 不应产生重复副作用。

## Run 查询

- `GET /api/v1/runs/{run_id}`

Run 用于记录一次 Agent 调用的生命周期，包括调用输入、调用状态、结果摘要、错误信息和事件序列。

## Plan

计划接口用于表达需要用户确认或分步执行的 Agent 操作。后端提供轻量同步 PlanExecutor，用于执行可后端调用的步骤；遇到 UI Handoff、缺少输入或外部 Agent Runtime 时，会返回 `next_action` 并暂停。

- `GET /api/v1/plans/{plan_id}`
- `POST /api/v1/plans/{plan_id}/actions`
- `POST /api/v1/plans/{plan_id}/execute`
- `POST /api/v1/plans/{plan_id}/confirm-and-execute`
- `POST /api/v1/plans/{plan_id}/resume`

当前支持的 Plan Action：

- `confirm`：确认继续执行。
- `cancel`：取消计划。

执行策略：

- `return_plan_only`：只返回计划。
- `require_confirmation`：确认后执行。
- `auto_execute`：路由后自动执行可执行步骤。
- `host_managed`：由宿主应用推进。

`next_action` 常见类型：

- `confirm_plan`：等待用户确认。
- `open_ui`：需要宿主应用打开页面。
- `collect_input`：需要补充参数。
- `wait_for_agent_event`：等待外部 Agent 或宿主系统上报事件。
- `none`：无后续动作。

## Session

- `GET /api/v1/sessions/{session_id}/messages`
- `POST /api/v1/sessions/{session_id}/messages`

Session API 用于保存 Host 展示和上下文消息，是可丢弃、可重建的读模型；它不是 Canonical Turn、
Run、Result、Plan 或 Memory 的事实源。删除、刷新或重新加载展示消息不会隐式重开已完成 Turn，
也不能用消息历史覆盖 OIR 的语义运行状态。MVP 只提供轻量会话消息能力，不实现复杂会话状态机。

`POST /api/v1/sessions/{session_id}/messages` 用于宿主应用写入 `/route` 之外产生的可见聊天消息，例如子 Agent 回复。请求字段保持通用：

```json
{
  "source": "agent_chat",
  "role": "agent",
  "content": "子 Agent 返回给用户的消息",
  "agent_id": "summarizer",
  "agent_session_id": "child_session_1",
  "request_id": "req_1",
  "event_id": "event_1",
  "metadata": {}
}
```

当 `source=agent_chat` 时必须提供 `agent_id`；`agent_session_id` 可选，用于宿主侧区分同一 Agent 的子会话。后续同一 session 且 `current_agent.agent_id` 相同的路由请求会把这些消息作为 Agent history Context Item 参与预算选择。
## Agent Entitlement 授权

通用 `UserContext` 可携带 `entitlements: string[]`，Agent `access_policy` 可配置 `any_entitlements: string[]`。两者只接受安全 ASCII 值，去空、去重并稳定排序，按完整字符串精确匹配。`any_entitlements` 内部为 OR，与其他非空 Policy 维度为 AND；空数组保持旧 Policy 兼容。

面向 LLM 的 `CandidateAgent` 不包含 Principal claims、签名或完整 AccessPolicy。受权管理视图的 `AgentPublic` 保留 `access_policy.any_entitlements` 供审计与配置管理。
