## Context

Memory Debug API 先按调用方提供的 request/session/turn/run 等条件查询 `memory_events`，再独立查询能够关联这些 source turns 的 Formation Jobs。Lifecycle Decision Event 由 `MemoryLifecycleOperation` 创建，只稳定携带 tenant、user、formation job、memory key/ID 和 decision status；它不天然属于 Job 覆盖的某一个 request 或 turn。结果是 Job trace 可以被找到，但传给 `_trace_view` 的 request-filtered events 不包含其 Decision Event，导致 `MemoryFormationDecisionView.decision_id` 和 `proposed_operation` 无法恢复。

前端当前把 `decision_status=pending && decision_id` 作为操作区出现条件，并只允许 `proposed_operation=update/delete` 确认。这个权限模型与管理服务一致，但缺失关联被静默表现为没有按钮。运行图也只把完整 Memory Request Trace 压缩为“沉淀本次记忆”一个节点，主要依赖 `overall_stage` 和关联 ID；pending conflict 关联已有 current memory 时，旧 memory/index refs 可能让节点看起来已经持久化，即使本轮候选仍等待人工处理。

本 change 面向开发调试人员和非技术演示受众。它必须保持 tenant/user ownership、bounded projection、敏感候选脱敏、现有 ADD 不可确认规则以及“一次性 route-and-invoke + per-turn Memory Trace polling”的架构边界。

## Goals / Non-Goals

**Goals:**

- 让 request/session/turn/run 关联查询返回可操作 pending UPDATE/DELETE 所需的现有 `decision_id` 和 `proposed_operation`。
- 让所有具有 `decision_id` 的 unresolved pending decision 能被拒绝，同时为关联缺失提供明确诊断。
- 保持顶层 Events 的筛选语义、租户/用户隔离和脱敏边界。
- 在现有运行图中提供可展开的 Memory Formation 子流程，并区分自动处理中、人工待处理、完成、跳过和失败。
- 根据 accepted lifecycle decisions 判断本轮是否真正写入，不把关联已有 current memory 当成本轮持久化成功。

**Non-Goals:**

- 不允许确认 pending ADD，也不改变后端只将 UPDATE/DELETE 作为可确认 `proposed_operation` 的规则。
- 不改变 Formation model、semantic validator、verifier、confidence threshold、hard rules 或 lifecycle semantics。
- 不修复 mem0 provider timeout；本 change 只保证 Recall 错误与 Formation 状态在 UI 中归属正确。
- 不新增统一追踪契约、SSE/WebSocket、数据库字段、迁移或新的管理 API。
- 不把 Memory 子流程升级为服务端精确实时执行跨度；请求返回前仍不推测内部模块进度。

## Decisions

### Decision 1: 分离调用方筛选事件与 Trace 支撑事件

`debug_state` 继续构建严格遵守调用方 filters 的 `events`，用于响应中的顶层 Events、Context Trace links 和 request-level evidence。Formation traces 确定后，服务使用每个已授权 trace 的 `tenant_id + user_id + formation_job_id` 补查 bounded Job events，并将它们作为独立的 `trace_events` 传给 `_trace_view`。

`trace_events` 不并入响应的顶层 `events`，也不用于扩大 caller filter 命中的 items。这样既能恢复历史 Decision Event，又不会让 `request_id=A` 的事件列表包含一个多 Turn Job 下属于其他 request 的事件。

首版允许对 bounded traces 使用 `asyncio.gather` 并行补查，每个查询保持 repository limit。若真实管理查询出现明显 N+1 成本，再增加 tenant-scoped `formation_job_ids` 批量查询；不为本次修复提前扩大 repository contract。

替代方案：给 Decision Event 回填 request/turn/run。一个 idle/window Job 可覆盖多个 Turn，选择单个 source association 会制造错误归属，并且需要迁移历史数据，因此不采用。

### Decision 2: 用 operation_id 恢复 Decision Event，但保留现有操作授权矩阵

`_trace_view` 只接受与当前 trace job、tenant 和 user 匹配的 Job events，并继续用 `payload.operation.operation_id` 对齐 `trace.operations[].operation_id`。匹配后恢复 `decision_id`；只有 pending candidate 的原始 `proposed_operation` 为 UPDATE 或 DELETE 时才填充可确认操作。ADD、敏感/已脱敏或无法恢复的候选保持 `proposed_operation=null`。

前端操作矩阵固定为：

| Decision | 确认 | 拒绝 |
|---|---:|---:|
| pending UPDATE | 是 | 是 |
| pending DELETE | 是，二次确认 | 是 |
| pending ADD/其他且有 decision_id | 否 | 是 |
| pending 但缺少 decision_id | 否 | 否，显示诊断 |

管理 API 继续执行 ownership、expected revision 和 idempotency 校验。前端不根据 memory_id 或 reason code推断可确认操作。

替代方案：把 ambiguous ADD 自动转换为 UPDATE。该方案改变 Policy/Lifecycle 语义，并可能覆盖 current memory，不符合本 change 范围。

### Decision 3: 操作区不再静默消失

Memory tab 将 pending decision 的“可见性”和“可确认性”分开判断。只要 status 为 pending，就展示决策处理区域；存在 `decision_id` 时至少提供拒绝，eligible UPDATE/DELETE 再提供确认。缺少 `decision_id` 时显示安全、可操作的关联错误和刷新建议，不暴露候选正文或内部 payload。

确认或拒绝成功后沿用现有 `onRefresh` 重新查询当前 Turn Memory Trace；有 index operation 时继续使用现有 operation status 刷新，不增加新的轮询协议。

### Decision 4: Memory 主节点使用可展开子流程而不是增加同级主节点

运行图保留“收到问题 → 准备参考信息 → 中控判断 → Agent → 返回结果 → 沉淀本次记忆”的可扫描骨架。“沉淀本次记忆”节点增加可展开 `substeps`，使用现有 selected-turn `request_trace`、`formation_traces` 和 decisions 投影：

1. 收集本轮对话
2. 进入后台队列
3. 提取记忆候选
4. 语义校验与形成决策
5. 等待人工处理（仅存在 unresolved pending 时）
6. 更新长期记忆
7. 更新检索索引

展开状态属于 UI，不改变选中 Turn，也不自动打开技术 JSON。完整字段仍在 Memory tab 和节点详情中。

替代方案：把所有子阶段作为同级 Journey 节点永久显示。该方案会让窄右栏过长、削弱领导演示的主链路，因此不采用。

### Decision 5: 为人工 pending 增加前端 `attention` 状态

Memory 子流程增加 `attention`（待处理）视觉状态。它与 `active`（系统仍在处理）分开：Job pending/claimed/retry 或 index pending/retry 为 active；unresolved Decision pending 为 attention。主 Memory 节点存在 attention 子步骤时，摘要优先显示“有 N 条记忆变更待处理”，不能仅因 request trace 关联到 ready current memory 而显示“记忆已就绪”。

是否发生本轮持久化必须依据 accepted ADD/UPDATE/DELETE decision 及其 revision/index outcome，而不是只检查 `memory_ids`、`revision_id` 或 `index_status` 是否存在。NOOP、REJECT、PENDING 关联到旧 memory 时不算本轮写入。

### Decision 6: Recall 错误保持在参考信息阶段

`memoryContext.errors` 中的 `provider_timeout` 属于请求前 Recall。运行图在“准备参考信息”节点展示“历史记忆读取超时/未使用”，而 Formation 子流程只解释响应后的写入形成。Recall 失败不隐藏、禁用或覆盖可操作 Formation Decision。

## Risks / Trade-offs

- [Risk] Job 级辅助查询增加 Debug 请求数据库访问次数。 → 保持 trace 和 event bounded、并行查询；使用真实数据观察后再决定是否批量化。
- [Risk] 一个多 Turn Job 会在多个 source turn 的 Trace 中显示同一 pending decision。 → 这是 Job 级形成语义；UI 显示相同 job/decision ID，管理 API 的幂等性防止重复处理。
- [Risk] 敏感 pending candidate 无法恢复 eligible proposed operation。 → 保持不可确认，只允许在拥有安全 decision_id 时拒绝；不以降低脱敏为代价恢复按钮。
- [Risk] 前端子流程可能被误解为请求返回前的精确实时追踪。 → 继续遵循现有 Journey 规则，请求未返回时内部子阶段保持等待，返回后才按 Trace 还原。
- [Trade-off] pending ADD 仍不能确认，具体冲突只能拒绝或保持 pending。 → 这是明确的产品边界；未来若需要人工将 ADD 转为 UPDATE，单独设计 revision-safe 管理契约。

## Migration Plan

1. 增加后端回归测试，复现 request-only/turn-only 查询找到 Job 但丢失 Decision Event 的问题。
2. 实现 bounded Job event hydration，并验证顶层 event filters、tenant/user isolation 和历史 Event 兼容性。
3. 调整 Memory tab 操作区与缺失关联诊断，补齐 pending UPDATE/DELETE/ADD 前端测试。
4. 扩展 Journey projection、状态模型和渲染，补齐 pending、accepted、noop/reject、retry/dead-letter 与 Recall timeout 测试。
5. 使用真实 mem0/PostgreSQL 配置复测冲突场景、操作完成后的 Trace 刷新和桌面/窄屏布局。

回滚可独立移除前端子流程与后端 Job event hydration；没有 schema migration 或新持久化数据需要回退。

## Open Questions

- 暂无阻塞问题。pending ADD 的人工确认明确不在本 change 中。
