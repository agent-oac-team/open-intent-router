## Context

当前 `RouterService.route()` 会在请求入口创建 Canonical Turn。Route-only 和 Plan 路径具有部分 Turn 状态迁移，但 `POST /api/v1/route-and-invoke` 在获得 RouteResponse 后直接调用 `InvocationService.invoke_from_route()` 并返回；InvocationService 单独持久化 Run 和 Result，既不把 Run 关联到 Turn，也不调用已有的 `TurnService.complete_with_result()`。结果是 Agent 执行成功时仍可能出现 Canonical Turn=`pending`、Result=`completed`、Outbox 缺失的部分成功状态。

系统同时保留旧的 direct Turn Capture、structured Run/Result projection 和新的 Canonical Turn Outbox 三条形成入口。它们的目标不同却缺少明确的权威边界：direct capture 可以绕过 Canonical Turn，Canonical Outbox 又可能永远没有生成；Formation mode 关闭时还会把 Run 标记为 suppressed，使后续配置开启无法解释原始决策。测试台只按 request ID 轮询 Formation Trace，连续 20 次没有 trace 就切为 `not_triggered`，掩盖了上游 Turn 未收口的问题。

该修复涉及 API 编排、事务仓储、异步 Outbox、Formation/Index worker、历史数据对账和前端状态契约。外部 Agent 调用可能耗时或失败，不能放进数据库事务；Memory Formation 和 mem0 indexing 必须继续异步，不能增加主对话响应时延或把 Provider 可用性变成主请求成功条件。

## Goals / Non-Goals

**Goals:**

- 让每个可信 `route-and-invoke` 请求都有可验证的 Canonical Turn 状态，并在返回终态响应前可靠关联 Run/Result、完成 Turn 和写入 Outbox。
- 消除 Result 成功、Turn pending、Outbox 缺失的部分状态，并保证重试、并发和服务重启下的幂等恢复。
- 让 completed Canonical Turn Outbox 成为会话偏好/事实 Formation 的唯一入口，同时保留结构化 Run/Result task projection 的独立职责。
- 在 enforced 模式跑通 Candidate -> Policy -> Lifecycle -> Item/Revision -> Index Operation -> mem0 -> Recall 闭环，并为每一阶段提供可关联状态。
- 让 off/observe/enforced 与 temporary/private 策略有稳定、可审计、不会被后续配置变化改写的语义。
- 为已有滞留 Turn 提供 dry-run 对账和受控修复，为测试台提供不会消失的明确终态。

**Non-Goals:**

- 不改变 RouteResponse、AgentInvocationResult 或现有 Host API 的对外主字段。
- 不在主请求中同步等待 Formation Model、Memory Lifecycle worker 或 mem0 indexing。
- 不把全部 Plan、delegated external run 或历史 IRS/OAC 数据迁移问题纳入本次修复。
- 不无条件回填在 Formation off、temporary 或 private 策略下产生的历史内容。
- 不用关键词或正则为“记住/喜欢”增加直接写入旁路；自然语言仍由 Formation Model 生成严格候选。

## Decisions

### 1. 为 Canonical invocation 增加两阶段事务协调端口

增加内部 `CanonicalInvocationStore`（名称可按现有 repository 风格调整），提供两个数据库事务操作：

1. `start_run(turn_owner, run, expected_turn_version)`：插入预生成 Run，并把 Turn 从 `pending` 更新为 `running`、关联 run ID。
2. `complete_run(turn_owner, completed_run, result, final_response, expected_turn_version, formation_policy_snapshot)`：更新 Run、插入 Result、完成 Turn，并插入唯一 `turn.completed` Outbox。

InvocationService 在调用 invoker 前执行第一个短事务，事务提交后再进行外部调用；invoker 返回后执行第二个短事务。内存实现使用同一锁模拟原子性，PostgreSQL 实现使用同一 session/transaction 和条件更新。API 返回模型保持不变，内部持久化结果需携带 `result_id` 和 Turn 完成信息供调试。

选择该方案而不是让 API handler 顺序调用 `attach_activity()`、`add_result()`、`complete_with_result()`，因为顺序调用无法保证 Result/Turn/Outbox 原子性，正是当前部分成功的来源。也不把外部 Agent 调用包在一个长事务内，以避免锁占用、连接耗尽和不可控回滚。

### 2. Canonical 请求只走 Turn Outbox 会话形成路径

`route-and-invoke`、`route-and-execute` 中已创建 Canonical Turn 的调用将标记 `canonical_turn_managed=true`（内部上下文，不信任 Host 输入）。InvocationService 对这类调用禁用 `TurnCaptureService.capture()` 的 legacy direct capture。completed Turn Outbox consumer 构造唯一 Formation Turn，并沿现有窗口/idle coordinator 创建 Job。

structured Run/Result projector 仍可生成执行状态类 `task_memory`，因为其语义来源是 canonical execution state；它不得生成或替代用户偏好/稳定事实，也不得把 Result summary 当作会话 Turn。通过 source type、memory key 和 idempotency key 保持两类投影边界。

备选方案是同时保留 direct capture 和 Outbox，以“谁先成功谁算”为准；该方案会造成双 Formation Turn、重复候选与难以证明的幂等关系，因此拒绝。

### 3. 在 Outbox 固化 Formation 资格证据

`turn.completed` payload 保存有界的 `formation_eligibility`：mode（off/observe/enforced）、suppressed boolean、reason code、policy version 和 execution mode，不保存凭证或额外用户正文。Consumer 同时执行当前运行时 kill switch 与事件资格判断：任何一方禁止 side effect 时记录明确 skipped 事件并终结该 Outbox，不写 Memory。

这使服务重启后仍能解释原始决策，也防止把历史 off/private 请求在当前 enforced 配置下静默回填。对于当时 enabled、当前临时 off 的事件，按 mode-off 语义记录 skipped，而不是无限积压；如未来需要可恢复暂停，应另行定义 pause 模式。

### 4. Formation 与 indexing 使用分阶段终态

后端 trace 投影统一输出以下高层阶段：

- `turn_pending` / `turn_running`
- `outbox_pending` / `formation_skipped`
- `formation_pending` / `formation_retry` / `formation_dead_letter`
- `completed_no_candidate` / `policy_rejected`
- `memory_persisted_index_pending` / `index_retry` / `index_dead_letter`
- `persisted`

`persisted` 仅表示 canonical Item/Revision active 且 index operation ready；存在 Item 但 index 未完成时不得提前报告全链路成功。现有底层 job/status 保持兼容，高层阶段由 observability service 根据 canonical 记录投影，不把 UI 逻辑作为事实源。

### 5. 增加 orphan Turn reconciler 和显式修复命令

新增只读扫描器按 tenant/user/request 关联 pending/running Turn 与唯一终态 Run/Result，输出 `repairable_enabled`、`repairable_skipped`、`ambiguous`、`ownership_conflict`。执行模式必须显式指定范围和幂等 key：

- enabled 且所有权唯一：完成 Turn、补唯一 Outbox。
- suppressed/off/private：完成 Turn并写 skipped 审计，不创建可形成 Outbox side effect。
- 关联不唯一/所有权冲突：不修改，进入人工清单。

默认 dry-run，禁止扫描时输出完整用户正文。该 reconciler 也可作为低频 runtime health 检查，但自动修复默认关闭。

### 6. API/UI 通过后端高层 trace 判断终态

扩展 Memory Debug Response，按 request ID 返回 Canonical Turn、Run/Result、Outbox、Formation、Lifecycle、Index 和 Recall 的有界关联摘要与 `overall_stage`、`terminal`、`retryable`、`reason_code`。前端只根据这些字段调度轮询：终态停止，非终态采用有上限的退避；达到主动轮询上限后保留最后状态和手动刷新，不推断 `not_triggered`。

备选方案是单纯增加前端轮询次数；它无法修复缺失 Outbox，也会把永久 pending 变成更慢的误报，因此拒绝。

### 7. 端到端测试分为确定性门禁和真实 Provider smoke

CI 使用真实 PostgreSQL 事务、确定性 Formation Model fake 和可验证的 mem0/index adapter fake，覆盖 API 到 recall 的完整状态和失败注入；本地 smoke 可使用 Milvus Lite + 配置的真实 Formation Provider，验证严格 JSON 重试、真实 embedding/index 和后续 recall。UI 测试使用后端契约 fixture 覆盖所有高层阶段。

验收主用例使用“访前准备 + 我喜欢吃猪肉”的多意图输入，断言路由/Agent 业务结果正常、偏好形成不被业务意图吞掉、Memory Item/Revision/index ready，且下一次同用户允许 scope 的调用能召回该偏好。

## Risks / Trade-offs

- [终态事务改造影响 InvocationService 的既有仓储调用] -> 先引入可选事务端口并保持 direct invoke 旧路径，逐步把 Canonical 路径切换后用契约测试锁定兼容响应。
- [Run 已启动后进程崩溃会留下 running Turn] -> 使用现有 deadline/heartbeat 思路增加 stale run/turn reconciler，且终态重放以 request/run/result/turn version 幂等。
- [同时保留 structured task projection 可能让调试台看到多个相关 Job] -> 用 source type 和高层 trace 分组，明确 conversational formation 与 execution projection，不按 request ID 粗略合并成同一 Job。
- [策略快照与当前 kill switch 组合可能让事件永久 skipped] -> 明确 off 是终态而非 pause；若需要暂停恢复，后续单独增加 pause 语义。
- [历史 pending Turn 缺少完整策略快照] -> 以 `formation_suppressed`、turn_captured、request policy audit 等保守证据分类；证据不足时不自动写 Memory。
- [真实 Provider 输出仍可能偶发不符合严格 schema] -> 保留 retry/dead-letter、完整错误码和确定性 CI；真实 smoke 不作为无凭证环境的阻塞门禁。
- [Debug 扩展可能泄露用户偏好正文] -> 默认只返回有界 preview、hash、IDs 和安全 reason code，沿用身份校验与 redaction。

## Migration Plan

1. 先增加 schema/索引和事务端口，实现但不切换主路径；运行数据库迁移、仓储并发与回滚测试。
2. 增加后端高层 trace 和 orphan dry-run 报告，盘点现有 pending Turn、缺失 Outbox 和 suppressed 记录。
3. 在本地/测试环境把 Canonical route-and-invoke 切到两阶段事务协调，禁用该路径的 legacy direct capture，保留 feature flag 便于回滚。
4. 运行 PostgreSQL E2E、worker/index 重启恢复、UI 状态测试和真实 Provider smoke，确认 request -> recall 闭环。
5. 对历史记录先执行 dry-run；只修复证据明确的 enabled 记录，对 suppressed/off/private 记录写 skipped 审计，不自动回填。
6. 观察 pending Turn 数、Outbox lag、Formation dead-letter、index out-of-sync 和 trace_missing 指标后移除临时双读兼容。

回滚时关闭新的 Canonical invocation feature flag，恢复旧 Invocation persistence，但保留已提交的 Turn/Outbox 和新字段；Outbox/Formation worker 继续依赖幂等键，MUST NOT 删除已形成 Memory 或回退数据库迁移中的事实记录。

## Open Questions

- `route-and-execute` 的单 Agent 非 Plan 分支是否与 `route-and-invoke` 同批切换；设计建议同批复用事务端口，避免保留第二个缺口。
- explicit `POST /invoke` 是否要求调用方提供/创建 Canonical Turn；本次默认维持 direct invoke 语义，仅修复已有 Canonical 路由请求。
- 当前 mode=`off` 被定义为永久 skipped；若运维需要“暂时暂停、恢复后继续消费”，应新增独立 `paused` 模式而不是复用 off。
