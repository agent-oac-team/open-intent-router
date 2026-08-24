# API 概览

> OAC 使用的 IRS 兼容 API 由独立 Host Adapter 暴露，不属于 OIR Native API。
> 完整路径、身份、Ticket 和 capability 契约见 [OAC Host Adapter](../architecture/oac-host-adapter.md)。

本文档说明 `open-intent-router` MVP 阶段提供的主要接口边界。接口以 FastAPI 暴露，完整字段定义以代码中的 Pydantic Schema 和运行时 OpenAPI 文档为准。

## Native 身份与所有权

Native Route、Invoke、Plan、Run、Session 和个性化 Agent 查询使用单一 Principal Envelope：

- `X-OIR-Principal-Envelope`：base64url 编码的规范 JSON，至少包含
  `claims_version=oir-principal-v1`、`subject`、`tenant`、`roles`、`groups`、
  `entitlements` 和 `attributes`。
- `X-OIR-Principal-Signature`：使用 `NATIVE_PRINCIPAL_SECRET` 对 Envelope 原文计算的
  HMAC-SHA256 十六进制签名。

非 local 环境必须验签；`APP_ENV=local` 只允许 loopback 请求使用无签名 Envelope。迁移期仍接受
`X-User-ID`、`X-Tenant-ID` 和 `X-Memory-Identity-Signature`，但它们只能形成空
roles/groups/entitlements 的 owner-only Principal。请求体中的用户字段只做 subject/tenant
一致性校验，不能提供或扩大权限。

Run、Plan 和 Session 的 Native 读写都以 Principal 的 `(tenant, subject)` 查询；跨用户、跨租户
和不存在目标统一返回 `404`，admin-like role 不隐式绕过 owner endpoint。公开且脱敏的
`GET /api/v1/agents` 与 `GET /api/v1/agents/{agent_id}` 仍无需 Principal；
`POST /api/v1/agents/available` 是个性化查询，必须认证。

## 健康检查

- `GET /health`：仅表示服务进程存活；不触发 Registry、Runtime Adapter 或外部依赖探测。
- `GET /ready`：服务就绪检查。每次应用 lifespan 都创建新的单次 Application Runtime，先构建
  Runtime Catalog，再建立 Primary Registry 和所需的受管数据库目标。受信 source mapper 在
  lifespan、Admin Registry reload 或已提交的受信 Registry 写入后，在同一 source-refresh fence
  内原子加载/替换当前 Snapshot。请求仅刷新已激活 Adapter 的有界健康观察与每个唯一受管数据库的
  有界 probe，不重载 Registry 或创建数据库资源。Catalog 激活失败时应用只发布降级 View，不创建
  Database、Container 或后台 Runtime，`/health` 仍为 `200`，`/ready` 返回
  `runtime_catalog_unavailable`。完整运行态中数据库 probe 的短暂失败返回
  `database_unavailable`，View 未发布或已关闭返回 `application_runtime_unavailable`。Catalog、
  Primary Registry 或部署标记为 required 的 Adapter 失败同样返回 `503`，失败响应固定为
  `{ "status": "error", "runtime_status": "error", "runtime_reason": "..." }`。响应不暴露
  Adapter 配置、endpoint、数据库 URL、凭据或原始异常。可选 Adapter 失败时返回 `200` 和
  `{ "status": "degraded", "runtime_status": "degraded", "reason_code":
  "runtime_adapter_unhealthy", "impacted_definition_count": n }`；只有依赖该 Adapter 的 v2
  Definition 会被当前 Snapshot 隔离。

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

一次受信请求只形成一个 Candidate Set。Direct Invoke 先从该集合选择目标再产生 Context、Run、
Result 或 Invoker 副作用；route-and-invoke 复用 Route 已生成的候选 ID，不重复执行访问策略。
Plan 的 confirm、execute、confirm-and-execute 和 resume 是不同请求，因此各自重新形成一次
Candidate Set，并在该请求的全部 Step 预检和执行中复用；任一非终态 Step 的 Agent 不可用时返回
`404 agent_not_available`，Plan 保持不变。

对于从 v2 Registry Snapshot 创建的 Plan，OIR 在每个 Step 的内部持久状态中冻结所选 Agent
revision 与安全的声明式 Binding Requirement；它不保存历史权限结论、Adapter/Client 实例或凭证，且
不会出现在 Plan API 或 OpenAPI 响应契约中。execute、resume，以及 `plan_control`/`agent_event`
的受控路由均从当前 Snapshot 重新选择并比较该 Binding。revision、Requirement 或
Runtime Binding 不兼容时，返回 `503 plan_binding_unavailable` 和安全 `reason_code`，不创建新的
Run、Delegated Run 或 Execution Ticket，也不静默替换处理方式、Adapter 或 owner。

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

Agent 知识预取默认关闭。只有 `context.knowledge.mode=prefetch` 时才会在调用前检索知识；
`context.knowledge.mode=controlled_retrieval` 用于固定工作流节点按模板调用检索，不允许模型
任意决定检索。Knowledge Requirement 默认为 `optional`；`required` 在 Provider 缺失或
失败时以 `knowledge_unavailable`、在治理后空结果时以 `knowledge_not_found` 阻止 Agent
调用。`controlled_retrieval + required` 的 direct invoke 需要同时提交
`knowledge_context_handle` 与 `knowledge_context_trace_id`，调用方自报的
`knowledge_context` 不能替代受信 Handle。Router 阶段 Memory 只在 `MEMORY_MODE=on` 时启用
并固定 scopes 为 `user_preference,stable_fact`；Knowledge 仍由显式 route policy 控制。

完整 Knowledge 正文只存在于当前进程内的 `AgentInvocation`，不会进入跨请求缓存。
Route-only 的 `InvocationPreview` 不执行 Knowledge 检索，也不返回 Knowledge Context 正文；
`route-and-invoke` 在真正进入 Invocation 时才检索并把正文交给目标 Agent。
Run、Result 中显式命名的 Knowledge Context、Agent/Conversation Event 和 Route Log 在
持久化模型入口统一投影为状态、`trace_id`、`item_id`、`source_id`、数量、截断标记和
稳定错误码；Citation 的持久化最小形态只有 `source_id`。`summary`、`content`、title、
URI 和任意 Provider metadata 不进入上述长期记录，也不会成为 Memory Formation 输入。
当前同步执行链无需异步正文载荷；未来若引入异步执行，必须另行实现加密、短 TTL、终态
删除的临时交付，而不能复用 Run/Event/Trace。

同一个 Route/Invoke 流程使用 request-scoped assembly cache。Router 检索候选可被目标 Agent 复用，但 Agent 阶段必须重新应用 Agent visibility、声明 source/scope 和预算；不同请求、用户或租户之间不复用。

OIR 可通过 `knowledge_sys` HTTP Adapter 实现该 Provider 端口。配置
`KNOWLEDGE_PROVIDER_BASE_URL` 后，OIR 使用 `RS256` 短时 JWT 直连
`/api/v1/knowledge/search`；Base URL 为空时不构造 Provider。JWT 默认
`iss=oir`、`aud=knowledge_sys`、TTL 60 秒且最长不超过 300 秒，只包含
`knowledge:read` Scope，并传播受信 Tenant、Principal 与 Trace。签发私钥使用
`KNOWLEDGE_PROVIDER_JWT_PRIVATE_KEY` 或
`KNOWLEDGE_PROVIDER_JWT_PRIVATE_KEY_FILE` 二选一；只读
`GET /.well-known/jwks.json` 仅公开当前 `kid` 对应的 RSA `n/e`，不返回私钥参数。
远端 Base URL 必须使用 HTTPS；与 OIR 同机的 PM2 部署允许
`localhost`、`127.0.0.1` 或 `::1` 的 loopback HTTP。

Adapter 总 Deadline 默认 12 秒且不自动重试。30 秒窗口内 5 次 timeout、
unavailable 或 5xx 会打开 Circuit 30 秒，窗口结束后只允许一个 half-open 探测；
empty、denied、JWT 4xx 和业务 4xx 不计入故障。Deadline、Circuit 窗口、阈值和打开
时间均可由同名前缀的 `KNOWLEDGE_PROVIDER_*` 环境变量覆盖。

### `POST /api/v1/route-and-invoke`

先执行路由，再在目标 Agent 可调用且输入满足要求时执行调用。

适用场景：

- 宿主应用希望后端直接完成“识别意图 + 调用工具”。
- 目标 Agent 的 `handling.kind=invocation` 已由当前 Registry Snapshot 解析为受支持的 v2 Runtime
  Adapter Binding。
- 调用过程需要统一记录 Agent Run、事件和结果。

如果目标 Agent 只能由前端或宿主系统处理，例如 `ui_handoff` 或 `external_execution`，接口会返回
相应的 Host 协作动作，而不会伪装为本地调用。

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

只有 `handling.kind=invocation` 且 Snapshot Binding 已解析为部署注册的 v2 Runtime Adapter 时，
该接口才会执行。Adapter Key、Connector 与配置都是部署/Definition 的受治理逻辑引用；Native
Runtime 不会按旧 `type` 选择 `mock`、`http` 或 `local_function` Invoker，也不会把 Binding 缺失
猜测为外部委派。`ui_handoff` 与 `external_execution` 由路由或 Plan 返回 Host 协作动作，不是
direct-invoke 目标。

Direct Invoke 在 Binding、输入和受治理 Context 预检通过后才受理：它先短事务创建 `running` Run，
在事务外调用 Runtime Adapter，再短事务原子写入终态 Run 和唯一 Result。Adapter 的已受理失败以
`AgentInvocationResult` 的稳定安全错误码返回；Binding 或必需依赖在受理前不可用仍使用既有错误
信封且不创建 Run。提交确认丢失时 Runtime 仅回读完全匹配的 Run/Result，不会自动重调 Adapter。
已受理 Adapter 的取消异常同样收敛为安全失败，不将它表述为已停止的远端副作用。Direct Invoke
没有调用方幂等承诺；重复 HTTP 请求始终创建新的 Run。

若冻结的 Runtime Adapter Binding 含 `connector_ref`，Core 在创建 Run 前调用部署提供的窄
Connector Resolver。该 Resolver 只能使用 deployment policy、已绑定 Native Principal 的 tenant/身份、
冻结的 `adapter_key` 和逻辑 `connector_ref`；它不重新选择 Handling 或 Adapter。返回的 Resolved Connector
可携带 endpoint、credentials 或短时 client，但作为请求级私有值与 Envelope 分开传给 Adapter，并在成功、
预检拒绝、Adapter 失败、deadline 或取消后释放。缺失、越权、Adapter/ref/revision 不匹配或不可用时返回
既有 `invocation_binding_unavailable` 的安全 `503`，不创建 Run、Result、Ticket 或远端调用；公开响应、
Trace 和日志不输出 Connector 的 endpoint、Header、Token 或 Secret。
Adapter 若吞掉 deadline 的取消，Core 会继续强持有它直至应用生命周期排空；为了不让该 Adapter 使用已关闭
的私有 capability，Connector 的 release 在该迟到任务结束时完成，而不是在安全 deadline 响应返回时抢先关闭。

内置 `http` Runtime Adapter 仅使用 HTTP Connector 中由 deployment 选择的 endpoint、method 与显式
允许的认证 Header；它复用 Catalog 生命周期内唯一的 Async Client，不会临时创建 Client。默认只接受
HTTPS、精确 host/port allowlist 与 TLS 验证；Definition 或调用方不能透传 URL、Host/Header、Token 或
网络策略。请求与响应应用硬大小上限，redirect 默认失败；部署显式启用 redirect 时每一跳都重新校验完整
Egress Policy。HTTP status、协议异常、非 JSON 和畸形 JSON 都收敛为既有安全 Invocation 失败，不向
公共结果暴露远端 body 或异常文本。

预检会构造唯一的 Agent Call Envelope。它只含只读执行 ID、Definition `input_schema` 已声明并通过
校验的输入、默认 `subject/tenant` Principal、三方门控后可选的规范 claim、安全 Context 定位事实、
有界 Artifact reference、绝对 deadline，以及已存在的可信 Plan 幂等键。Token、Header、完整
Principal attributes、完整 Definition、内部 context / Trace / Route metadata、凭据和
Memory/Knowledge 正文都不会传给 Adapter。所有调用都受部署级 `INVOCATION_*` 上限约束，Definition
只能收紧；输入、Context、Artifact 或可确定为空的 required Context 的预检失败返回安全 `422` 且不创建
Run；required Context Provider/受控 Handle 不可用时同样在受理前以稳定 `knowledge_unavailable` 失败。
已受理后发现 message、structured output、Artifact 或 usage 非法/超限则收敛为
`invocation_invalid_response`。Artifact 的 metadata 是闭合安全投影：仅 `size_bytes`、`content_type`
和 `sha256`；title 只能是逻辑 locator。`artifact://`/`memory://` URI 是无 path 的 opaque locator，
HTTPS URI 只能使用无 port/query/fragment/credentials 的安全 authority/path segments，不能传递正文、
Header 或凭据。deadline 到达时 Core
立即返回安全失败，即使 Adapter 吞掉取消并在后台迟到完成也不会延长该调用或覆盖终态。

请求中的 `memory_context` 或 `knowledge_context` 只是 Core 组装受治理 Context 的候选；v2 Runtime
Adapter 不会沿用其完整对象。`controlled_retrieval + required` 只接受与当前 tenant、principal、Agent、
Source Scope 和 trace 匹配的短时单次 Handle，不能由调用方正文绕过。

## Memory

Memory API 用于 M5 记忆召回、低风险写入候选处理、TTL 清理和调试检查。mem0 作为可选策略层隐藏在 adapter 后面；OIR 仍负责租户启用、scope、TTL、可见性、权限、审计和 Agent 可见范围。

- `POST /api/v1/memories/recall`：按 `query`、`user`、`scopes`、`subject`、`agent_id` 和 `max_items` 召回记忆，返回 `MemoryRecallResponse.context`。
- `POST /api/v1/memories/write-candidates`：提交候选记忆，服务根据置信度、敏感标记、scope TTL 等策略返回 accepted/rejected 决策。
- `POST /api/v1/memories/cleanup`：清理已过期记忆，并记录过期事件。
- `GET /api/v1/memories/debug`：按 user、tenant、agent、scope 或 request ID 查看当前可见记忆项、写入/过期事件、mem0 provider 状态、外部 ID 映射和最近错误摘要。按 request 查询时额外返回 `request_trace`，包含 `overall_stage/terminal/retryable/reason_code` 及 Turn、Run/Result、Outbox、Formation、Memory/Revision、Index 的有界 ID 关联。正文和 Provider 凭证不会进入该高层 trace。
- `GET /api/v1/runtime/config`：返回脱敏后的有效 Memory SQL/Milvus/embedding
  配置及 `memory_infrastructure_sources`。接口不返回数据库密码、API key 或 Milvus token。
- `GET /api/v1/user-memories`：OAC Host V2 认证后的个人产品读接口。主体只取签名 Principal；固定每页 20 条，支持 `page` 与可选 `memory_type=user_preference|stable_fact`，按更新时间倒序返回 active 用户偏好和稳定事实。响应只含正文、产品类型、可用状态、更新时间、分页信息，以及前端不展示的目标令牌和并发令牌；不返回内部 ID、置信度、Provider、索引操作、dead-letter 或 Revision 历史。
- `DELETE /api/v1/user-memories/{target_token}`：OAC Host V2 当前 Principal 的单目标产品删除接口。请求只接受稳定 `idempotency_key` 和列表返回的 `concurrency_token`；服务端解析目标令牌后重新校验 tenant、user、subject、scope 与版本。跨主体和不存在目标统一返回 `404`，版本变化返回 `409`。受理响应只返回 `accepted` 与 `idempotent_replay`；目标在受理事务中立即 fail-closed，从个人列表和后续 Recall 排除，异步清理状态不进入产品响应。
- `GET /api/v1/admin/memories/governance`：仅接受 `oac_admin` Host 凭证，按 `tenant_id` 查询删除异常工作队列；无 `memory_id` 时按 `page/page_size` 分页，并可用 `status=needs_attention|blocked|repairing` 在分页前筛选，指定 `memory_id` 时返回单条详情。每项同时返回 `repairable`，缺少权威删除操作等无法安全推导动作的条目只能查看。普通删除等待满 300 秒才进入，删除 dead-letter、确认的 Provider 残留和外部删除完成但 Canonical 未收口立即进入；Formation、普通索引不同步和 Recall 质量不进入。本接口是独立产品读模型，不复用 Memory Debug。
- `POST /api/v1/admin/memories/governance/{memory_id}/repair`：使用具备 `control_write` 的管理员 Host 身份提交单目标治理修复。请求必须携带稳定 `idempotency_key`、查询返回的 `expected_version` 与 `expected_anomaly`；服务端在提交时重读 Canonical Item 和删除操作，只会确定性选择安全清理推进或 Canonical 收口。状态或版本变化、目标不可修复时返回 `accepted=false` 和安全原因，不执行副作用；受理只表示进入 `repairing`，不表示修复完成。

`request_trace.overall_stage` 的主要值为 `turn_pending`、`turn_running`、`outbox_pending`、
`formation_skipped`、`formation_pending/retry/dead_letter`、`completed_no_candidate`、
`policy_rejected`、`memory_persisted_index_pending`、`index_retry/dead_letter`、`persisted` 和
`trace_missing`。`persisted` 仅表示 canonical Item/Revision active 且 index operation ready。

真实 mem0 记忆闭环使用 `MEMORY_STRATEGY_PROVIDER=mem0` 开启，本地 Milvus 可使用 Milvus Lite。Memory SQL、Milvus 和 embedding 只读取显式 `MEMORY_DATABASE_URL`、`MEMORY_MILVUS_*`、`MEMORY_EMBEDDING_*` 配置；缺少必要配置时启动失败，不回退 Knowledge、通用 Embedding 或 Router LLM 配置。OIR 会把 mem0 add/search/delete history 和 `memory_id`/`mem0_memory_id` 映射写入 PostgreSQL-backed `memory_events`，不把 mem0 SDK 内部 SQLite history 当作长期事实来源。完整配置、失败策略、Mermaid 流程和 smoke 路径见 [mem0-memory-integration.md](../architecture/mem0-memory-integration.md)。

记忆 scope 包括：`user_preference`、`stable_fact`、`task_memory`、`artifact_reference`、`session_summary`。其中 `task_memory`、`artifact_reference`、`session_summary` 默认 14 天过期；用户偏好和稳定事实默认长期保留。

## Knowledge Provider

OIR 不暴露 Knowledge Search、Read、Assets、Chunks、Admin、Index、ACL、Cache 或 Audit
HTTP 入口，也不代理或回退这些请求。Agent Definition 中的
`context.knowledge.source_ids` 只是传给 Provider 的逻辑来源范围，不是最终授权证明；
物理资产解析、ACL 与审计由外部 Knowledge Provider 负责。OIR 仅在 Agent Invocation
期间瞬时消费 Provider 返回的 `knowledge_context`。

## Agent 查询

查询接口：

- `GET /api/v1/agents`
- `GET /api/v1/agents/{agent_id}`
- `POST /api/v1/agents/available`

三个响应都会隐藏密钥、Header Token 和私有调用参数等敏感配置。前两个是公开脱敏 Catalog；
`available` 使用已验证 Principal 的角色、用户组、租户、entitlement 和属性过滤，忽略请求体中的
权限声明。

Native Registry 只接受 `schema_version: oir-agent-v2` 的 Definition。Native API、文件 Registry
和数据库 Registry 都不会再根据 `type`、`invocation`、`ui_handoff`、`provider_config` 或
`metadata` 推断执行方式；携带这些旧字段的请求以 `422` 拒绝。不会新增平行的 `/v2` URL：现有
`/api/v1/agents` 即为唯一 Native v2 契约。

公开 `GET /api/v1/agents` 与 `GET /api/v1/agents/{agent_id}` 仅返回安全的公共字段和
`handling_kind`（`invocation`、`external_execution` 或 `ui_handoff`），不返回 Handling、
Adapter、Connector、Executor、endpoint、Header 或配置参数。

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
- `GET /api/v1/admin/runtime/inventory`

`POST` 和 `PUT` 的请求与响应使用完整 `AgentDefinitionV2`；`PATCH enabled` 返回公共投影。
Definition 的唯一执行声明为 discriminator `handling`：

```json
{
  "schema_version": "oir-agent-v2",
  "agent_id": "script_writer",
  "name": "话术生成",
  "description": "生成客户沟通话术",
  "handling": {
    "kind": "invocation",
    "adapter_key": "copywriter_adapter",
    "connector_ref": "tenant_copywriter",
    "config": {"function": "generate"}
  }
}
```

`handling.kind=external_execution` 使用逻辑 `executor_ref` 和受限 `params`；
`handling.kind=ui_handoff` 使用内部绝对路径 `route` 和受限 `params`。所有引用都是逻辑标识，
不是 endpoint、凭据或任意部署配置。`GET /api/v1/admin/agents` 是管理投影：它返回
`handling_kind` 与全量脱敏的 `handling`，用于查看和选择三种 Handling；被脱敏的字符串无法
round-trip，管理 UI 在保存前必须由操作者重新输入相应安全引用。这样管理员可管理定义而不会经由
读接口取得部署秘密。

说明：

- `database` 模式支持完整 CRUD。
- `file` 模式主要用于只读加载，不适合运行时变更。
- `hybrid` 模式以数据库为主，数据库不可用时才使用本地文件兜底。
- Runtime inventory 只向已认证管理员输出 v2 Definition 的 revision、enabled、Handling kind、
  全量脱敏后的 Handling、binding 状态和安全隔离原因，以及聚合 quarantine/受影响计数。每个
  `quarantined_definitions` 条目只包含 `source_index`、可选的已校验逻辑 `agent_id` 和封闭的
  `reason_code`（当前为 `definition_schema_invalid`、`definition_configuration_invalid`、
  `duplicate_agent_id` 或 `legacy_definition_unmappable`）；不能安全校验的 locator 返回 `null`。
  不输出 Connector reference、Adapter key、executor/endpoint、Header、凭据或原始异常。公开 Catalog
  和 Candidate 投影仍只输出安全的 `handling_kind`，不包含这些诊断字段。

## 事件

事件接口用于接收外部 Agent 对已存在 Delegated Run 的进度和终态事实。

- `POST /api/v1/events/agent`
- `POST /api/v1/runs/{run_id}/events`

两条 Native Event 路径都必须携带 `X-OIR-Execution-Ticket`。服务验证 Ticket 的签名、存储
hash、purpose=`agent_event` 和过期时间，并从 Ticket claims 派生 run、turn、owner、agent、
plan 和 step；请求中的任一同名字段冲突都会在写入前拒绝。Native API 不提供 Ticket 签发端点，
Ticket 由创建 Delegated Run 的受信内部调用方签发。

进度 Event 在命令提交后 release Ticket，以便后续进度继续使用；`agent_result`、`agent_error` 和
`agent_cancelled` 在 Run/Turn/可选 Plan Step/Event/Outbox 原子提交后 consume Ticket。相同 Event ID
重放返回 `duplicate=true`，不复制 Result、Event 或 Outbox；拒绝路径不产生部分业务写入。内部
Invoker 仍直接调用应用服务，不通过 HTTP 或重复验证 Ticket。

## Run 查询

- `GET /api/v1/runs/{run_id}`

Run 用于记录一次 Agent 调用的生命周期，包括调用输入、调用状态、结果摘要、错误信息和事件序列。
Delegated Run 到达 `deadline_at` 后由 lifespan 管理的 sweeper 自动提交幂等 timeout 命令；扫描只看
deadline，不因 heartbeat 陈旧而提前 timeout 或重试。终态竞争依赖 expected state version，迟到方
不会覆盖已经提交的 completed、failed、cancelled 或 timed_out 状态。

## Plan

计划接口用于表达需要用户确认或分步执行的 Agent 操作。后端提供轻量同步 PlanExecutor，用于执行可后端调用的步骤；遇到 UI Handoff、缺少输入或外部 Agent Runtime 时，会返回 `next_action` 并暂停。

- `GET /api/v1/plans/{plan_id}`
- `POST /api/v1/plans/{plan_id}/actions`
- `POST /api/v1/plans/{plan_id}/execute`
- `POST /api/v1/plans/{plan_id}/confirm-and-execute`
- `POST /api/v1/plans/{plan_id}/resume`

当前支持的 Plan Action：

- `confirm`：确认继续执行。
- `cancel`：取消尚未开始且没有活动 Run 的计划工作。若存在活动 Delegated Run 且 Runtime 没有真实
  下行控制通道，返回 HTTP `200`、`accepted=false`、`transitioned=false`、
  `reason_code=control_unsupported` 和未变化的 Plan/state version；不会伪造 `cancel_pending` 或
  `cancelled`。

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

Plan DAG 以“`pending` 且所有依赖都为 `completed`”定义 ready Step。Executor 每次结果后重算完整
ready-set，并按 Plan 原始 Step 数组顺序串行选择；`current_step_id` 不 ready 时不会阻塞其他 ready
Step。只有全部 Step completed 才能完成 Plan；不可恢复失败立即 fail-fast，未开始 Step 保持
pending，声明的 blocked 状态保持 blocked。若 Plan 未完成却不存在可解释的 ready/blocked 状态，
服务返回状态不变量冲突，不伪造完成。

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

## OAC 业务运行观察 Host Adapter

以下入口属于 OAC Host Adapter，不是 OIR Native API。它们只接受受信 V2 Host 身份；Adapter 从已验证身份绑定 tenant 和 user，不信任请求体或游标声明的所有权。OAC Go 必须先确认本地 OAC Session 仍存在且属于当前登录用户，随后才能代理这些入口。

- `GET /api/v1/runtime-observation/sessions/{session_id}/turns/{turn_id}`：返回当前 owner 的 Execution Trace Snapshot。事件按 `event_offset` 升序排列；`watermark` 覆盖全部返回事件；`completeness`、`incomplete_reason_codes` 和 `recovered` 明确观察完整性与恢复状态。无所属 Trace 返回 `404 execution_trace_not_found`。
- `GET /api/v1/runtime-observation/sessions/{session_id}/turns/{turn_id}/events`：SSE 增量流。客户端在 Snapshot 后以其已渲染的 `watermark` 作为 `Last-Event-ID`；每条 `execution_trace` 的 SSE `id` 等于 `event_offset`，服务端只发送大于该游标的同 owner 事件。流首条 `execution_trace_meta` 提供完整性元数据；每次建连均重新执行身份和所有权范围校验。
- `POST /api/v1/runtime-observation/sessions/{session_id}/turns/{turn_id}/handoffs`：由受信 OAC Host 报告已经发生的 `ui_handoff` 事实。`requested`、`completed` 和 `failed` 使用稳定 `handoff_id` 和状态组成来源幂等身份；该入口不提供暂停、重跑、改写或删除 Trace 的控制能力。Trace 写入失败时仍返回 `accepted=true`，并同时返回 `observation_status=incomplete` 与安全原因码。
- `GET /api/v1/runtime-observation/sessions/{session_id}/turns/{turn_id}/memory-decisions/{decision_id}/evidence`：按当前受信 owner 和所属 Turn 读取一条 pending Decision 的旧值/新值业务证据。Memory 正文不进入 Execution Trace `facts`，只在该受保护入口按需返回。
- `POST /api/v1/runtime-observation/sessions/{session_id}/turns/{turn_id}/memory-decisions/{decision_id}/{action}`：确认或拒绝当前 Trace 中仍为 `pending` 的 Governed Memory 决策，`action` 只允许 `confirm` 或 `reject`。请求必须提供稳定 `idempotency_key` 和 `reason`，可用 `expected_revision_id` 做乐观并发校验。Adapter 先校验 Turn owner 和 pending Decision，再调用权威 Memory Management 应用端口；成功响应返回 operation、Memory/Decision/Index ID、Provider 状态、是否幂等重放及 `observation_status`。Trace 投影失败不改判 Memory 操作结果，但响应必须标记 `incomplete`。不存在返回 `404`，并发或幂等冲突返回 `409`。该入口不会建立第二套 Memory 状态机。

Execution Trace 是 append-only 的观察投影，不是 Turn、Run、Result、Memory 或页面状态机。事件采用受限事实白名单，不能保存 Provider 原始 Payload、Prompt、凭证、授权值、未脱敏错误、Memory 正文或完整上下文。新写入只接受 `schema_version>=2`，其中 `memory_decision` 明确拒绝 `previous_value` 和 `proposed_value`；读取时仍兼容测试环境已经保存的 v1 事件，但 OAC Host Adapter 在 Snapshot 与 SSE 输出中统一剥离这两个旧字段。旧值和新值只能通过上述受所有权保护的 Evidence 入口按需读取。已有 v1 来源事实以 v2 重放时，幂等比较会归一化 schema 版本并忽略旧 Memory 正文字段；其他关联、状态或事实差异仍按来源冲突拒绝。投影失败不会回滚已提交的业务事实；当前写响应、Snapshot 和 SSE 会以 `observation_status/completeness=incomplete` 暴露已知缺口。

`source_repaired` 是带真实 offset 的 `trace_integrity` 补写事件，只说明来源投影已修复，不会把 Snapshot 标记为 `recovered=true`。只有查询时从已有终态 Canonical Turn 重建终态观察摘要，Snapshot 才返回 `recovered=true`，并提供有界 `recovered_state={source:"canonical_turn",status,outcome,state_version}`。该恢复不合成中间事件，也不把不完整 Trace 改判为完整。

## OAC Plan Host Adapter

以下入口属于 OAC Host Adapter，只接受受信 V2 User Host 身份。Plan 的 `status`、`state_version`、当前 Step 和 `next_action` 均来自 OIR Canonical Plan，OAC Session 消息和浏览器本地状态不得覆盖这些字段。

- `GET /api/v1/central/active-plan?session_id={session_id}`：返回当前 owner 在该 OIR Session 中最新的 `pending/running/blocked` Plan；没有 Active Plan 时返回 `plan=null`。用于刷新、跨标签页和跨设备恢复，不返回其他用户或租户的 Plan。
- `POST /api/v1/central/plans/{plan_id}/confirm`：请求体必须包含稳定 `request_id` 和 `expected_state_version`，成功转换时将 `request_id` 记录为确认事件身份。同一确认身份的重放仍被识别为该转换的 owner，允许宿主从响应中断处继续；其他并发、陈旧或终态请求返回最新 Canonical Plan，并以 `conflict=true` 阻止宿主再次创建受控路由或 Provider 副作用。
- `POST /api/v1/central/events/agent`：Plan Step 完成动作可携带稳定 `event_id` 和 `expected_state_version`。Adapter 在领取 Execution Ticket 前校验 owner、Plan、当前 Step 与版本；陈旧或终态动作返回 `conflict=true` 和最新 Canonical Plan，不消费 Ticket、不更新 Run/Turn/Trace。未携带版本的旧客户端仍遵循唯一受信关联规则。
- `POST /api/v1/central/route` 的 `source=plan_control|agent_event`：对当前 Step 按 Registry 定义投影唯一协作动作。UI Handoff 返回 `blocked + open_ui`；缺少输入返回 `blocked + collect_input`；外部 Agent 返回 `blocked + wait_for_agent_event`。终态 Plan 直接返回 Canonical 完成、失败或取消状态，不重新交给 LLM 判断。

兼容 Plan 响应包含 `status`、`state_version` 和 `next_action`。`next_action` 是唯一 Host 协作指令；终态必须清除陈旧动作。本期 OAC Host 未实现 Plan Step 重试契约，即使收到未知重试 metadata 也不展示重试按钮。

## Agent Entitlement 授权

通用 `UserContext` 可携带 `entitlements: string[]`，Agent `access_policy` 可配置 `any_entitlements: string[]`。两者只接受安全 ASCII 值，去空、去重并稳定排序，按完整字符串精确匹配。`any_entitlements` 内部为 OR，与其他非空 Policy 维度为 AND；空数组保持旧 Policy 兼容。

面向 LLM 的 `CandidateAgent` 不包含 Principal claims、签名或完整 AccessPolicy。受权管理视图的 `AgentPublic` 保留 `access_policy.any_entitlements` 供审计与配置管理。
