## Why

`route-and-invoke` 当前会创建 Canonical Turn，却没有在 Agent Result 持久化后关联 Run/Result、完成 Turn 并写入 Transactional Outbox；当 Formation 配置关闭或链路异常时，请求可能长期停在 `pending`，测试台轮询结束后又只显示 `not_triggered`，导致用户表达的长期偏好既未进入记忆，也缺少可操作的失败证据。现在需要把请求接受、Agent 执行、Turn 收口、异步 Formation、Memory 持久化与前端反馈连成可恢复、可验证的完整闭环。

## What Changes

- 为 `route-and-invoke` 建立 Canonical Turn、Run、Result 与最终语义响应的完整状态迁移，在返回成功响应前持久化已完成 Turn 及其 Outbox 事件。
- 使用事务协调器原子提交 Run/Result/Turn/Outbox，消除“Result 已成功但 Turn 仍 pending”的部分成功状态，并通过所有权、版本与幂等约束处理重试和并发。
- 将 completed Canonical Turn + Transactional Outbox 设为会话自动记忆形成的唯一权威入口；对同一请求停止重复走 legacy direct capture，避免漏写或双写。
- 保证 `off`、`observe`、`enforced` 三种 Formation 模式不影响 Canonical Turn 正常收口；关闭时记录明确的 skipped 原因，开启时异步完成 candidate、policy、lifecycle、revision、mem0 index 全链路。
- 增加孤儿/滞留 Turn 对账与受控修复：可识别已有终态 Result 但 Turn 未完成的记录，按原始策略快照决定补发 Formation 或只完成审计，禁止无条件回填长期记忆。
- 改进 Memory Trace 状态契约和测试台展示，使 `pending`、`skipped`、`retry`、`dead_letter`、`completed_no_candidate`、`persisted` 等结果保持可见，并展示关联 request/turn/run/result/job/memory 标识与安全错误码。
- 增加真实 PostgreSQL 与 API/UI 端到端回归测试，覆盖“访前准备 + 我喜欢吃猪肉”这类多意图输入，验证偏好最终形成、持久化、索引并可被后续召回。

## Capabilities

### New Capabilities

- `route-invoke-memory-completion`: 定义 route-and-invoke 从 Canonical Turn 收口、Outbox 投递到 Memory Item/Revision/Index/Recall 的可靠闭环、恢复与端到端验收。

### Modified Capabilities

- `agent-invocation`: Agent Run/Result 持久化需要与所属 Canonical Turn 的活动关联和最终收口保持原子、一致、幂等。
- `memory-context-governance`: 自动记忆形成仅消费 completed Canonical Turn，并明确 Formation mode、跳过审计、重复投递和后续召回行为。
- `status-inspector-panel`: Memory 状态检查器需要显示持久且可解释的形成终态、关联标识和错误原因，不再在固定轮询次数后把未知状态折叠为未触发。

## Impact

- 后端：`app/api/router.py`、`app/services/invocation_service.py`、`app/services/turn_service.py`、Turn/Run/Result 事务仓储、Turn Outbox consumer、Formation worker、Memory observability。
- 数据：`canonical_turns`、`agent_runs`、`agent_results`、`turn_outbox`、`memory_formation_turns/jobs`、`memory_items/revisions/index_operations/events`；可能增加策略快照、收口/对账状态字段或索引。
- API：保持现有 `route-and-invoke` 响应兼容；扩展 Memory Debug/Trace 状态与关联信息，不暴露敏感正文或 Provider 凭证。
- 前端：`web/src/App.tsx` 的逐轮 Memory Trace 轮询与状态展示，以及对应类型和测试。
- 测试：补充事务失败、幂等重试、服务重启、Formation mode、模型重试/死信、mem0 索引和后续 recall 的集成与端到端用例。
