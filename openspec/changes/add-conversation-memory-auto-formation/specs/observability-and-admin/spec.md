## ADDED Requirements

### Requirement: Memory Debug API 支持形成与生命周期关联查询
系统 SHALL 允许授权调用方按 request、session、turn、run、formation job、memory key/ID、tenant、user、agent、scope 和 decision status 查询 bounded memory formation/lifecycle state。

#### Scenario: 按 formation job 查询
- **WHEN** Debug 调用方提供 `formation_job_id`
- **THEN** API 返回该 job 的 trigger、source range、versions、status、attempt、bounded decisions、revision refs 和 provider/index outcomes

#### Scenario: 按 turn 查询异步形成结果
- **WHEN** Debug 调用方提供 `turn_id` 或 request ID
- **THEN** API 返回包含该 source turn 的 Recall Used 和已完成/待处理 formation decisions，而不是返回无关联的全局 items 冒充本轮结果

#### Scenario: 组合 tenant/user filters
- **WHEN** 调用方同时提供 tenant、user 和 scope filters
- **THEN** repository 在授权边界内组合过滤 items、revisions、jobs 和 events，不跨 tenant 扫描后再在客户端过滤

### Requirement: Formation Trace 解释每个候选和 lifecycle operation
系统 SHALL 为每个 formation job 记录可关联、bounded 且版本化的 trace，覆盖 ADD、UPDATE、DELETE、NOOP、REJECT、PENDING 和 provider/index 状态。

#### Scenario: Job 完成并产生多个 decision
- **WHEN** formation job 完成
- **THEN** trace 包含 trigger、source refs/range、extractor/projector/policy version、candidate/decision counts、reason codes、memory/revision refs、latency/usage 和 final status

#### Scenario: Job 失败或重试
- **WHEN** model、policy persistence、worker lease 或 provider operation 失败
- **THEN** trace 显示 attempt、retry/dead-letter 状态、bounded error code 和 last update，且不改变已完成 Agent response

#### Scenario: Context Trace 与 Formation Trace 关联
- **WHEN** 某 turn recall 了 memory，随后该 turn 又参与形成
- **THEN** 两类 trace 通过 request/session/turn/run/memory refs 关联，但 MUST NOT 将完整 debug/formation payload 注入 Router 或 Agent model input

### Requirement: Memory 调试和审计默认不暴露敏感正文
系统 SHALL 对 formation turns、candidates、rejected/pending decisions、revisions、events、provider errors 和 UI payload 应用 bounded projection 与 redaction。

#### Scenario: 敏感候选被拒绝
- **WHEN** DLP/policy 因 secret 或 regulated data 拒绝候选
- **THEN** persistent event 和 Debug API 只显示 reason、scope、stable refs 或不可逆 hash，MUST NOT 返回原始 value、quote 或完整 prompt

#### Scenario: Tombstone 被查询
- **WHEN** 用户或 TTL 删除完成后查询 memory events
- **THEN** tombstone 只包含 actor、reason、scope、operation/provider status 和 timestamps，不包含 current/revision content 或 structured value

#### Scenario: Runtime 形成状态可见
- **WHEN** runtime/debug API 暴露 formation worker、queue 或 mem0 状态
- **THEN** 只返回 mode、version、queue depth、last safe error、provider/collection/status 等非敏感信息，不返回 API key、token、password 或连接凭证

### Requirement: 用户和管理员可以受控管理 Memory lifecycle
系统 SHALL 提供与只读 Debug 查询分离的认证管理契约，用于删除自己的 memory、确认/拒绝 pending decision 和查看 operation 状态。

#### Scenario: 用户删除自己的 memory
- **WHEN** 已认证用户请求删除同 tenant/user/subject 的 memory
- **THEN** 系统验证 ownership、创建 idempotent deletion operation，并返回 pending/completed provider status

#### Scenario: 用户尝试删除其他主体 memory
- **WHEN** 非管理员请求删除无权管理的 tenant/user/subject memory
- **THEN** 系统拒绝请求，且 MUST NOT 暴露目标正文或存在性详情

#### Scenario: 管理员跨主体操作
- **WHEN** 有效 admin credential 对其他主体执行 delete/confirm/reject
- **THEN** 系统记录 actor、target、reason、idempotency key 和审计事件

#### Scenario: 确认 pending UPDATE
- **WHEN** 授权用户确认一个仍有效且目标 revision 未变化的 pending UPDATE
- **THEN** 系统重新验证 policy/precondition 后执行 revision update；precondition 已变化时返回 conflict 而不是覆盖新 current state

#### Scenario: 拒绝 pending decision
- **WHEN** 授权用户拒绝 pending UPDATE/DELETE
- **THEN** 系统将 decision 标记 rejected/resolved，不改变 current memory 或 provider vector

### Requirement: Formation 和 lifecycle 指标支持上线治理
系统 SHALL 暴露不含正文的运行指标，用于评估形成质量、可靠性、成本和删除完成情况。

#### Scenario: 形成指标聚合
- **WHEN** formation jobs 被处理
- **THEN** metrics 可按 trigger/scope/status 聚合 queue latency、model latency/usage、ADD/UPDATE/NOOP/REJECT/PENDING rate 和 retry/dead-letter count

#### Scenario: Index 一致性指标
- **WHEN** provider operation 或 repair 执行
- **THEN** metrics 暴露 pending/out-of-sync/repaired/orphan counts 和 operation latency

#### Scenario: 删除完成指标
- **WHEN** 用户或 TTL deletion operation 执行
- **THEN** metrics 暴露 pending age、success/failure/dead-letter count，但不使用 memory content 作为 label

