## Context

Native Route、Invoke 与 Plan API 已复用 `require_memory_actor`，但该依赖只验证 `user_id + tenant_id`，随后仍保留 body 中的 roles、groups、entitlements 和 attributes。Run、Session 与 Available Agents 入口还存在无认证或自报所有权路径，Native Agent Event 也没有把外部写入绑定到 Execution Ticket。与此同时，Plan Schema 已能拒绝重复 Step、未知依赖和环，但会把数组首项设为 `current_step_id`，PlanExecutor 在该项尚未 ready 时直接把整个 Plan 标记为完成。

Core 已有可复用基础：`AgentDefinition.is_available_to`、owner-scoped Plan/Turn 数据、Delegated Run 的 start/progress/complete/fail/timeout 存储、Execution Ticket claim/lease/consume、状态版本和 Transactional Outbox。OAC Host 使用 `create_oir_app` 组合 Native Runtime，因此 Core lifespan 中的单个维护 Runtime 可以同时覆盖 Native 与 OAC 进程。

本变更必须遵守以下约束：Core 保持宿主无关；OAC `OIR-HOST-V2` 和冻结 Legacy wire contract 不变；外部调用不持有数据库事务；同一受信请求内不重复执行 Agent 授权筛选；不以 P0 修复为名提前建设通用 ExecutionRuntime。

## Goals / Non-Goals

**Goals:**

- 从完整、可验证的 Native Principal 构造 User Context，消除 body 权限提升。
- 在 Native Run、Plan、Session 和 personalized Agent 查询上统一 Canonical Ownership。
- 让 Direct Invoke、Route 后调用与延迟 Plan 在各自请求边界只消费一次 Candidate Set。
- 让外部 Native Agent Event 只能通过绑定具体 Delegated Run 的 Execution Ticket 写入。
- 修复 DAG ready-set、确定性串行选择和严格终态判定。
- 补齐 Delegated Run 取消确认、控制不支持结果、自动 deadline 收敛及真实端口一致性。

**Non-Goals:**

- 不提取 `ExecutionRuntime`，不新增 `/executions`、`/controls`、控制轮询、Webhook 或消息总线。
- 不为当前不支持控制的 Runtime 增加不可达的 `cancel_pending` Schema 状态。
- 不增加并行 DAG、输入绑定 DSL、插件化 Agent Runtime、统一反馈状态机或 UI Handoff 重构。
- 不接入 OAuth/OIDC/JWT，不处理 Invoker/数据库 Engine 生命周期警告，不拆分开源包与 extras。

## Decisions

### 1. Native 身份使用单一 HMAC Principal Envelope

新增宿主无关的 `NativePrincipal` 与 `require_native_principal`。推荐 wire 形态为 `X-OIR-Principal-Envelope`（base64url 编码的规范 JSON）和 `X-OIR-Principal-Signature`（对 Envelope 原文计算 HMAC-SHA256）；Envelope 至少包含 `claims_version=oir-principal-v1`、subject、tenant、roles、groups、entitlements 和 attributes。服务端对集合字段规范化，对 attributes 要求 JSON object，并由 tenant 声明覆盖任何同名属性。

非 local 环境必须验签；local loopback 可使用无签名 Envelope。现有 `X-User-ID`、`X-Tenant-ID` 与 `X-Memory-Identity-Signature` 作为迁移输入继续可用，但只能构造空 roles/groups/entitlements 和仅含 tenant 的 attributes。body `user` 只可做 subject/tenant 一致性检查，其他权限字段一律丢弃。

未选择 OAuth/OIDC/JWT，因为 OIR 当前只需要验证受信网关注入的 Principal 投影，引入身份供应商、Token 生命周期和 JWKS 轮换会扩大本轮边界。未复用 `OIR-HOST-V2`，因为其 method/path/body/credential profile 属于 OAC Host 防腐层。

### 2. Candidate Set 以受信请求为生命周期

Registry 提供返回 `AgentDefinition` 的单次 available selection。Direct Invoke 在入口首次选择目标；Route-and-Invoke/Execute 使用 RouteResponse 中由 Core 生成的 candidate IDs 证明目标属于本次 Candidate Set；Plan 的 confirm、execute、confirm-and-execute 和 resume 每个 HTTP/Host 请求各形成一次 Candidate Set，并在该次执行循环中复用。

Invocation 内部接收已经选择的 Definition 或已授权 ID 集合，不在每个 Step 前重新计算 AccessPolicy。Plan 执行入口在产生任何副作用前，使用该请求的 Candidate Set 预检所有未终态 Step；任一目标不可用时返回稳定的 `404 agent_not_available`，Plan 保持不变。这样既关闭 Direct Invoke 绕过，也遵守“不在同一请求重复授权”的决定。Plan 创建与延迟确认属于不同请求，因此不能复用旧 Candidate Set。

### 3. 所有权在查询边界显式表达

Native API 从 Principal 提取 tenant/subject，并调用 owner-scoped service/repository 方法。Run 增加明确的 owner-scoped 查询入口；Session GET/POST 绑定 Principal，忽略 body/query owner；Plan 继续使用现有 owner-scoped repository。跨 tenant、跨 subject、不存在目标统一投影为 `404`。Admin API 不自动获得旁路，保持独立显式契约。

保留已文档化的脱敏公共 Agent Catalog；只有 personalized `/agents/available` 必须认证并从 Principal 构造 User Context，避免把本轮扩张为 Registry 产品契约重做。

### 4. Native 外部 Event 以 Execution Ticket 为唯一写入权威

Native Event API 从 `X-OIR-Execution-Ticket` 取得不透明凭证，验证签名、purpose、TTL 和存储记录后直接使用 Ticket claims 派生 run/turn/owner/agent/plan/step，任何请求字段不一致均拒绝。进度事件使用 claim 后写入并 release；completed、failed、cancelled 终态在 Core 命令成功后 consume。内部 Invoker 继续直接调用 Service，不经过 HTTP/Ticket。

Execution Ticket 的通用配置和依赖组装下沉到 Core，OAC Host 复用同一服务；OAC Legacy body 中的 Ticket 形态与唯一关联迁移逻辑保持在 Adapter。未增加 Native Ticket issue API，因为本轮不新增 Execution submission surface。

### 5. PlanExecutor 使用单一确定性 ready-set 规则

可执行 Step 定义为 `status=pending` 且全部依赖均为 `completed`。若当前 Step 不 ready，Executor 必须继续扫描 ready-set；多个 Step 同时 ready 时按 Plan 原始数组顺序选择第一个，并在每个结果后重新计算。Schema 继续在创建时拒绝重复 ID、未知依赖和环；初始 `current_step_id` 选择第一个 ready Step，而非数组首项。

只有全部 Step 为 `completed` 才写入 Plan `completed`。任一 Step failed 时 Plan fail-fast；等待输入、UI 或外部事件保持 blocked；其余“未完成且无 ready Step”抛出不变量冲突并保留 Canonical 状态，绝不伪造完成。未执行 Step 保持 pending，不新增 skipped/deadlocked 状态。

### 6. 取消请求与取消事实分离

`DelegatedRunService.cancel` 表示持有有效 Ticket 的执行参与方确认已取消，而不是 OIR 向下游发送控制。Memory/Database cancel store 以 event ID、状态版本和完整关联验证幂等地将 Run、Turn、可选 Plan Step 与 Outbox 原子收敛为 cancelled，并拒绝覆盖既有终态。

Plan 取消时先判断是否存在活动 Run。不存在活动执行时可直接取消未开始 Step；存在活动 Run 且没有真实下行控制能力时不改变状态，返回 HTTP 200 的 `PlanActionResponse(accepted=false, transitioned=false, reason_code=control_unsupported)`。未选择空壳 Dispatcher、`AgentInvoker.cancel` 或控制 Outbox，因为当前没有消费端，创建它们只会制造不可达状态。

### 7. Deadline sweeper 是轻量生命周期组件

新增可 start/stop 的 `DelegatedRunTimeoutRuntime`，由现有 FastAPI lifespan 与 Memory runtimes 一起管理。它按固定间隔批量获取 `deadline_at <= now` 的非终态 Delegated Run，为每个 `run_id + deadline_at` 生成确定性 timeout event ID，并调用现有幂等 timeout 命令。进度或终态竞争导致版本冲突时，本轮跳过并由下次扫描重读 Canonical 状态。

Heartbeat 陈旧仍可由 orphan query 暴露，但 sweeper 不据此提前 timeout 或自动重试。Runtime 默认启用并提供 interval/batch 配置；stop 必须等待后台 Task 退出，避免测试 event loop 关闭后的悬挂回调。

### 8. 端口一致性通过真实实现锁定

补齐 cancel store/Service 后，contract test 必须断言真实 `DelegatedRunService` 满足 `@runtime_checkable DelegatedRunApplicationPort`，而不只证明 `object()` 不满足。静态类型 CI 与 application-port 大规模重排留待独立变更。

## Risks / Trade-offs

- [Native Principal Envelope 是新的安全契约，旧客户端可能收到 401 或失去 body 权限] → 保留只含 owner 的旧 HMAC 迁移输入，更新本地测试 UI、示例和 API 文档；生产不提供恢复不受信 body 的开关。
- [HMAC Envelope 是网关身份投影而非完整请求签名，捕获后的重放保护弱于 OAC V2] → 仅用于受信内部边界并依赖 TLS/secret 隔离；若未来 Native API 直接暴露公网，再独立引入短时 Token/OIDC，不在 P0 混入半套协议。
- [请求级 Candidate Set 允许同一已开始执行请求内的权限变化不即时生效] → 把每次 confirm/execute/resume 视为新请求重新选择；运行中撤销使用显式 Execution Control，不在 Step 间制造竞态。
- [活动 Run 当前无法被用户取消] → 返回稳定 `control_unsupported` 而非虚假终态；真实双向控制由后续 ExecutionRuntime 设计解决。
- [Sweeper 与迟到 Event 竞争] → 依赖行锁、expected state version、终态不可改写和确定性 event ID；冲突后重读，不盲目重试外部副作用。
- [Owner-scoped repository 方法与旧内部无 owner 查询并存] → Native API 只允许经过 owner-scoped service；内部维护方法保持窄可见并以负向测试锁定边界。

## Migration Plan

1. 先增加 Principal Envelope Schema/验签、Core Ticket 依赖和配置，但在切换入口前更新测试 helper、本地 UI 与示例 Header。
2. 将 Route、Invoke、Available Agents、Plan、Run 和 Session Native 入口切到 Principal，并加入跨 owner 与 forged-claims 回归。
3. 将 Native Event 入口切到 Ticket claim/lease/consume；OAC Adapter 继续复用既有 Legacy 投影与冻结 fixture。
4. 修复 request-scoped Candidate Set 与 Plan ready-set，不与取消/timeout 存储改动混在同一个测试切片。
5. 实现 Delegated cancel store/Service、结构化 `control_unsupported` 和 deadline runtime，再接入 lifespan。
6. 更新 API/架构文档，运行完整 pytest、Ruff、文档 harness 与 OpenSpec strict validation。

安全入口切换后不通过配置回退到不受信 body。若发布必须回滚，回滚整个版本并恢复匹配的客户端；本变更不计划破坏数据库 Schema，已有 Ticket、Run 与 Plan 数据保持可读。

## Open Questions

无阻塞实现的问题。具体 Header 常量、默认 sweeper interval 和 batch size 可在不改变上述契约的前提下按现有配置风格确定。
