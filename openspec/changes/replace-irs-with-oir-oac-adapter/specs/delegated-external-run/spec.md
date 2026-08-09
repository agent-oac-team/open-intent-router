## ADDED Requirements

### Requirement: 外部 Agent 副作用开始前必须预创建 Delegated Run
系统 SHALL 在 Host 调用外部 Agent 之前创建绑定 Turn、Agent、tenant、user、session 与可选 Plan/Step 的 Delegated Run，并返回可用于后续回调的通用 execution reference。

#### Scenario: Route 选择 Host-managed Agent
- **WHEN** Route 决策需要 Host 打开或继续一个外部 Agent
- **THEN** OIR 先持久化 Delegated Run 和关联 Turn，然后才向 Host 返回可执行 handoff

#### Scenario: Run 预创建失败
- **WHEN** OIR 无法持久化 Delegated Run 或 execution reference
- **THEN** 系统不得返回可开始外部执行的成功 handoff

### Requirement: Execution Ticket 是最小权限且可过期的不透明凭证
系统 SHALL 将 Adapter Ticket 绑定到确定的 run/turn/owner/agent/plan/step/purpose/expiry/nonce，只存储 Ticket hash，且 MUST NOT 在日志、Trace、Diff、Debug 响应或业务正文中暴露 Ticket。

#### Scenario: 有效 Ticket 关联回调
- **WHEN** Agent Event 携带未过期、签名有效且所有权/用途匹配的 Ticket
- **THEN** Adapter 解析到唯一 execution reference 并向 Core 提交通用 Run 命令

#### Scenario: Ticket 过期、伪造或跨用户重放
- **WHEN** Ticket 签名无效、已过期、所有者不匹配或用于不同操作
- **THEN** Adapter 拒绝回调且不暴露对应 Run 的存在性或业务数据

### Requirement: 无 Ticket 过渡关联必须是唯一且可证明的
系统 SHALL 只在受信 request/event/plan/step 组合命中唯一未消费 Delegated Run 映射时接受无 Ticket 旧回调，MUST NOT 仅根据 `session_id + agent_id` 猜测 Run。

#### Scenario: 唯一过渡映射
- **WHEN** 旧客户端未回传 Ticket，但受信关联键只命中一个未消费 Run
- **THEN** Adapter 可接受该回调并记录 `correlation_mode=legacy_unique`

#### Scenario: 过渡映射零命中或多命中
- **WHEN** 受信关联键无法唯一确定未消费 Run
- **THEN** Adapter 返回可审计的关联错误，不更新任何 Run/Turn/Plan

### Requirement: Delegated Run 支持幂等进度与终态命令
系统 SHALL 使用 `event_id` 及 Run 状态版本处理 progress、completed、failed、blocked、cancelled 和 timed_out 命令，重复事件不得产生重复副作用。

#### Scenario: 重复进度 Event
- **WHEN** 同一 `event_id` 的 progress Event 重复提交
- **THEN** 系统返回原处理结果且不重复写入 Event、Result 或 Plan 状态

#### Scenario: 已完成 Run 收到冲突终态
- **WHEN** completed Run 收到后续 failed/cancelled 命令
- **THEN** 系统保留原终态并记录冲突，不改写 Result 或 Turn

### Requirement: Ticket 消费在 Core 成功提交后才完成
系统 SHALL 使用可恢复 claim/lease 处理 Ticket，并仅在 Core Run 命令幂等成功后标记 Ticket consumed，避免 Adapter 崩溃导致合法回调永久丢失。

#### Scenario: Core 提交前 Adapter 崩溃
- **WHEN** Ticket 已获取 claim 但 Core 命令未成功提交时进程中断
- **THEN** claim 在 lease 过期后可恢复，同一 Event 可安全幂等重试

#### Scenario: Core 已提交但 Adapter 未返回
- **WHEN** Core 已处理 Event 而 Adapter 在返回前中断
- **THEN** 重试通过 Core Event 幂等性获得相同结果，Ticket 最终收敛为 consumed

### Requirement: Delegated Run 终态与 Result/Plan/Turn/Outbox 原子收口
系统 SHALL 在单一 Core 事务中验证并更新 Run 终态、Agent Result、可选 Plan Step、Canonical Turn 和 Transactional Outbox。

#### Scenario: 最终 Agent Result 成功收口
- **WHEN** completed Event 通过 Run/Turn/Plan/Step 所有权与 Schema 校验
- **THEN** Run、Result、Plan Step、Turn 和 Outbox 在同一事务提交

#### Scenario: Plan Step 关联不匹配
- **WHEN** Event 携带的 Plan/Step/Agent 与预创建 Run 不一致
- **THEN** 整个收口失败且不写入部分 Result 或 Turn 终态

### Requirement: 超时与孤儿 Delegated Run 必须可治理
系统 SHALL 为 Delegated Run 记录 deadline、heartbeat/progress 和终止原因，并提供可观测的超时收敛与孤儿修复路径。

#### Scenario: Delegated Run 超过 deadline
- **WHEN** Run 在 deadline 后仍无最终 Event
- **THEN** 系统将其转为 timed_out 或待人工处置状态，对应 Turn 不进入完成 Formation

#### Scenario: 修复工具查看孤儿 Run
- **WHEN** 运维按时间、状态或 request/run ID 查询孤儿 Run
- **THEN** 系统返回脱敏关联和合法终止/重试选项，不绕过所有权或幂等校验
