## Why

按 request/turn 查看 Formation Trace 时，关联的 Decision Event 可能因为自身只携带 `formation_job_id` 而被 request 过滤排除，导致前端收到 pending operation 却缺少 `decision_id` 和 eligible `proposed_operation`，从而静默隐藏确认/拒绝操作。同时，运行图把异步记忆形成压缩成单一终态节点，无法解释排队、候选提取、语义校验、人工待处理、持久化和索引等真实阶段，甚至会把关联已有记忆的 pending conflict 误读为本轮已经写入完成。

## What Changes

- 在不改变 Memory Policy、Lifecycle 或 ADD 路线的前提下，让按 request/session/turn/run 查询得到的 Formation Trace 能从同租户、同用户、同 Job 的 Decision Event 恢复安全的 `decision_id` 和现有 UPDATE/DELETE `proposed_operation`。
- 保持顶层 Memory Events 严格遵守调用方筛选条件；Job 级辅助事件只用于构建授权范围内的 Formation Trace，不扩大 API 的事件结果集。
- 在 Memory inspector 中为 pending UPDATE/DELETE 恢复“确认”和“拒绝”，为其他可拒绝 pending decision 恢复“拒绝”，并在关联字段缺失时展示明确诊断而不是静默隐藏操作区。
- 保持 pending ADD 不可确认且不把 ADD 作为可确认的 `proposed_operation` 暴露；本 change 不修改候选策略、确认 API 或 Lifecycle 写入规则。
- 将运行图的“沉淀本次记忆”扩展为可展开的子流程，展示对话收集、后台投递、候选形成、语义/策略决策、人工待处理、长期记忆更新和检索索引状态。
- 使用独立的“待处理”状态表达人工 pending，避免把它误标为仍在自动运行、已经持久化或执行失败；Recall `provider_timeout` 继续属于“准备参考信息”阶段。

## Capabilities

### New Capabilities

<!-- None. -->

### Modified Capabilities

- `observability-and-admin`: 按 request/turn 查询 Formation Trace 时必须返回授权范围内可操作的 pending Decision 关联信息，同时保持事件筛选、脱敏和租户隔离。
- `status-inspector-panel`: Memory tab 必须正确展示 pending 决策操作，运行图必须提供可展开且忠实于现有 Trace 的记忆形成子流程。

## Impact

- 后端：`app/services/memory_observability.py` 及 Memory Event repository 的受限辅助查询路径；不修改数据库表、Formation Event 持久化格式或管理 API schema。
- 前端：`web/src/App.tsx`、`web/src/journey.ts`、相关类型、样式和测试；沿用现有 per-turn Memory Trace 轮询与节点详情交互。
- 测试：补充 request-only/turn-only Trace 关联、tenant/user 隔离、pending UPDATE/DELETE/ADD 操作矩阵、缺失关联诊断和记忆子流程状态投影。
- 兼容性：无 breaking API、无新依赖、无数据库迁移；已有历史 Decision Event 可通过 `formation_job_id` 被恢复。

