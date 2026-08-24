# Canonical Turn 与 Transactional Outbox 数据模型

本文档描述 OIR 对完整语义轮次的 canonical 数据边界。SQLAlchemy 模型以
`app/db/models.py` 为准，PostgreSQL 初始化与可重复迁移快照位于
`sql/postgresql_schema.sql`。

## 边界

Canonical Turn 表示一次可信用户输入及其最终语义结果。它不是 Host UI 消息行、聊天历史副本、
页面运行态或外部 Provider 会话。Host 可以保存或删除自己的展示消息，但不得据此重开、覆盖或
推断 OIR Turn 的终态。

Route 入口使用 `(tenant_id, user_id, request_id)` 作为业务幂等键，并额外保证 `request_id`
不能跨身份复用。相同请求只有在 session、source 和当前语义输入一致时才返回原 Turn。

## `canonical_turns`

主要字段：

| 字段 | 说明 |
| --- | --- |
| `turn_id` | OIR canonical 主键 |
| `tenant_id/user_id/session_id/request_id` | 可信所有权和幂等身份 |
| `source` | 通用请求来源，不包含宿主专有页面或 Provider 字段 |
| `status/state_version` | 状态与乐观并发版本 |
| `user_input_text` | 当前受控输入 JSON，不保存 Host 历史 |
| `references_text` | Route Decision、Run、Result、Plan、Event 引用 |
| `final_response_text` | 最终语义响应 JSON；活动 Turn 为空 |
| `created_at/updated_at/completed_at` | 生命周期时间 |

状态包括 `pending/routing/running/blocked/completed/failed/cancelled/timed_out`。
`completed/failed/cancelled/timed_out` 是终态，必须设置 `completed_at`，且不得被迟到 Event 重开。

关键约束和索引：

- `uq_canonical_turns_owner_request`：`tenant_id/user_id/request_id` 唯一。
- `request_id` 唯一索引：阻止同一请求 ID 跨身份复用。
- `idx_canonical_turns_owner_session_status`：按所有权、session 和状态查询活动 Turn。
- `idx_canonical_turns_status_updated`：超时和孤儿治理扫描。
- `idx_canonical_turns_owner_status_updated`：按 owner 范围执行滞留 Turn 对账。
- `idx_agent_runs_owner_request_status` / `idx_agent_results_owner_run_status`：在不跨身份扫描正文的前提下关联唯一终态 Run/Result。

## `turn_outbox`

Outbox 与 `canonical_turns.turn_id` 使用 `ON DELETE RESTRICT` 外键关联。主要字段包括
`event_type/idempotency_key/payload_text/status/attempt_count/max_attempts`，以及 claim lease、
可用时间、发布时间和最后错误码。

状态包括 `pending/claimed/retry/completed/dead_letter`。Worker 使用 owner + lease token 原子
claim；lease 过期后可恢复。相同 `idempotency_key` 只产生一个逻辑事件，重复完成返回既有状态。

## Plan Step 的 v2 Binding Fence

`plan_steps` 对 v2 Step 额外保存可空的 `agent_revision` 与 `binding_requirement_text`。二者只记录
经过 Schema 验证的声明式 Requirement，必须成对存在；旧 Plan 可为空以保持迁移期间可读。它们不保存
历史 entitlement、已激活 Adapter/Client 或原始凭证。每个延迟执行、恢复或受控路由都以当前 Registry
Snapshot 重新形成 Candidate Set 并比较这两个事实；不兼容时保持 Canonical Plan/Run/Turn 状态，不以
新 Binding 覆盖原 Step。数据库的 Plan 重写与 Delegated Run 的 Turn 终态事务都必须保留该字段对。

## 事务不变量

Route-only Turn 完成时，Turn 终态与 `turn.completed` Outbox 在同一事务提交。Canonical
invocation 使用两个短事务，外部 Agent 调用不持有数据库 transaction：

1. 先在 Core 内构造并校验 Agent Call Envelope，并为已冻结的 Runtime Adapter Binding 解析可选的
   请求级 Connector：只投影允许的输入、身份、Context reference、Artifact reference 与 deadline；
   Connector 的 endpoint/credentials 与 Envelope 分离，且必须与 tenant、Adapter key、逻辑 reference
   精确匹配。输入/Context/Artifact 上限、Connector 或 required Context 不满足时不创建 Run、不 claim
   Plan Step，也不调用 Adapter；预检通过后才 claim Step 并把可信 Plan 幂等事实附入同一 Envelope。
2. `start_run` 插入预生成 Run，并按 owner/state version 把 Turn 更新为 `running`、关联 Run。
3. Agent 调用在事务外执行；已受理的输出越界或非法时映射为安全 `invalid_response` 终态。
4. `complete_run` 更新终态 Run、插入 Result、完成 Turn 并插入唯一 Outbox。

非 Canonical 的 direct-invoke 同样保持短写入边界，但不创建 Turn 或 Outbox：先短事务写入
`running` Run，随后在事务外执行 Runtime Adapter，最后以一个短事务同时更新该 Run 并插入唯一
Result。终态提交确认丢失时，Runtime 只能按同一 `run_id` 和完全一致的 Result 读回已提交记录；
它不会再次调用 Adapter。direct-invoke 不提供调用方重放键，相同 HTTP 请求的再次调用仍创建新的
Run/Result。Adapter 在已受理后抛出取消异常也投影为安全失败并走同一终态收口；该投影不声称
远端副作用已经停止，后续控制能力只会在可验证时声明取消。

Delegated Run 最终完成时，以下写入也必须处于同一 PostgreSQL 事务：

1. 校验并更新 Run 终态。
2. 创建唯一 Agent Result。
3. 更新可选 Plan 和 Plan Step。
4. 完成 Canonical Turn。
5. 写入对应 Turn Outbox Event。

任一步失败都回滚全部写入。Memory Formation 在主事务提交后只消费 `turn.completed` Outbox，
并从数据库重新读取、校验 completed Canonical Turn 后构造 Turn Capsule。孤立 Message、Plan、Run、
Result 或 Event 不得直接触发自动 Formation；它们只能作为 Turn 的受信引用。主事务不得同步调用
Memory Provider，也不得让 Memory 失败反向修改已完成 Turn。

`turn.completed` payload 固化严格的 `formation_eligibility`：`mode`、`execution_mode`、
`suppressed`、安全 `reason_code` 和 `policy_version`。它不保存凭证或额外用户正文。Consumer 同时
检查该历史快照和当前 kill switch；历史 `off/private/temporary` 不会因为当前配置变为 enforced
而被静默回填。

## 滞留 Turn 对账

默认 dry-run：

```bash
.venv/bin/python scripts/reconcile_orphan_turns.py \
  --use-configured-database \
  --tenant-id <tenant> --user-id <user> \
  --updated-before <ISO-8601> \
  --report orphan-turns.json
```

报告只包含 IDs、状态和安全 reason code，分类为 `repairable_enabled`、`repairable_skipped`、
`ambiguous`、`ownership_conflict`。修改必须显式提供 request 范围、`--apply` 和
`--idempotency-key`。`repairable_enabled` 补 Turn/Outbox；suppressed/off/private 证据明确的记录
只完成 Turn 并写 `formation_skipped` 审计，不创建长期记忆副作用。
