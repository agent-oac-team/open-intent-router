## MODIFIED Requirements

### Requirement: mem0 provides memory strategy behind an adapter
The system SHALL use mem0 as the default memory strategy engine behind a memory adapter boundary, and automatic memory formation SHALL consume only completed, ownership-verified Canonical Conversation Turns rather than isolated messages, untrusted Host payloads, or partial Agent/Plan results.

#### Scenario: Memory is formed from a completed Canonical Turn
- **WHEN** a Canonical Turn containing trusted user input, final semantic response, ownership, and result references is completed and published through the transactional outbox
- **THEN** the system sends the governed Turn Capsule to the mem0-backed adapter for extraction, semantic update, or merge behavior

#### Scenario: Partial or untrusted runtime data is supplied
- **WHEN** a message, pending Run, isolated Agent Event, incomplete Plan result, or Adapter-assembled transcript is not backed by a completed ownership-verified Canonical Turn
- **THEN** the system MUST NOT use that data as an automatic memory formation input

#### Scenario: Memory is searched through strategy layer
- **WHEN** the system recalls memory for a route or invocation
- **THEN** it calls the memory adapter with user, subject, scope, metadata filters, query, and limit information

#### Scenario: Adapter result is governed by OIR
- **WHEN** mem0 returns candidate memories
- **THEN** open-intent-router still applies tenant enablement, Agent scope, permission, TTL, redaction, and budget rules before exposing them

## ADDED Requirements

### Requirement: Shadow Memory Formation 必须隔离于主召回数据域
系统 SHALL 在 State Rehearsal 中将 Memory Event、Item、Revision、Provider History 和向量写入独立测试 database/schema 与 collection，Decision Shadow MUST NOT 创建可被主链路召回的 Memory。

#### Scenario: Decision Shadow 产生可形成 Turn
- **WHEN** OIR Shadow 结果包含潜在记忆候选
- **THEN** 系统只记录无副作决策证据，不调用主 Memory Provider 或持久化主 Memory Item

#### Scenario: State Rehearsal 完成 Memory 闭环
- **WHEN** 隔离演练中的 Canonical Turn 完成
- **THEN** Formation/Write/Recall 只在隔离数据域可见，主召回数据域不得命中该记忆

### Requirement: Memory Formation 与 Recall 必须可独立 mode-off
系统 SHALL 提供无需修改 canonical Turn/Run/Result 数据的 Formation、Recall 和 Worker 独立关闭开关，并在关闭时保留可观测状态。

#### Scenario: Formation mode-off
- **WHEN** 运维关闭自动 Formation/Write
- **THEN** 新 Canonical Turn 仍正常完成且 Outbox/跳过原因可审计，但不新增或修改 Memory

#### Scenario: Recall mode-off
- **WHEN** 运维关闭 Memory Recall
- **THEN** Route 与 Invocation 继续使用无 Memory 的受治理 Context，且不放宽 Agent/Knowledge 权限

### Requirement: Canonical Turn Formation 的重复投递不得生成重复记忆副作用
系统 SHALL 以 turn ID、formation policy version 与形成窗口管理自动 Formation 幂等，并将重复 Outbox 投递收敛为同一逻辑决策/记忆操作。

#### Scenario: Formation Worker 重复消费同一 Turn
- **WHEN** 同一 completed Turn 的 Outbox 事件因至少一次投递而重复消费
- **THEN** 系统返回已处理的形成状态或幂等重算，不生成重复 Memory Revision/Provider Write

### Requirement: 干净切换不从 IRS 历史消息形成记忆
系统 SHALL NOT 导入 IRS/OAC 旧历史消息、Session、Plan、Result 或 Event 用于生成 OIR Memory；OIR Memory 仅从 cutover 后新建且已完成的 Canonical Turn 开始累积。

#### Scenario: 切换后 Memory 数据域初始化
- **WHEN** OIR 作为唯一事实源首次启动
- **THEN** Memory 数据域不包含基于 IRS/OAC 旧历史消息生成的 Item，后续只消费新 OIR Turn
