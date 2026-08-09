## MODIFIED Requirements

### Requirement: mem0 provides memory strategy behind an adapter
The system SHALL use mem0 as the default memory strategy engine behind a memory adapter boundary, and automatic conversational memory formation SHALL consume only completed, ownership-verified Canonical Conversation Turns delivered through the transactional outbox rather than isolated messages, untrusted Host payloads, partial Agent results, or a duplicate legacy capture path.

#### Scenario: Memory is formed from a completed Canonical Turn
- **WHEN** a Canonical Turn containing trusted user input, final semantic response, ownership, and result references is completed and published through the transactional outbox
- **THEN** the system sends the governed Turn Capsule through candidate validation, OIR policy/lifecycle persistence, and the mem0-backed index adapter

#### Scenario: Partial or untrusted runtime data is supplied
- **WHEN** a message, pending Run, isolated Agent Event, incomplete Plan result, or Adapter-assembled transcript is not backed by a completed ownership-verified Canonical Turn
- **THEN** the system MUST NOT use that data as an automatic conversational memory formation input

#### Scenario: Memory is searched through strategy layer
- **WHEN** the system recalls memory for a route or invocation
- **THEN** it calls the memory adapter with user, subject, scope, metadata filters, query, and limit information

#### Scenario: Adapter result is governed by OIR
- **WHEN** mem0 returns candidate memories
- **THEN** open-intent-router still applies tenant enablement, Agent scope, permission, TTL, redaction, and budget rules before exposing them

## ADDED Requirements

### Requirement: Formation 模式不得破坏 Canonical Turn 收口
系统 SHALL 在 `off`、`observe` 和 `enforced` 任一模式下正常完成 Canonical Turn 和 Outbox 审计；模式只控制 Formation 评估与 memory side effect，不得控制 Run/Result/Turn 的 canonical 持久化。

#### Scenario: Formation mode off
- **WHEN** 请求完成时 `memory_formation_mode=off`
- **THEN** Turn 和 Outbox 正常完成，consumer 记录 `formation_mode_off` skipped 终态且不创建或修改 Memory

#### Scenario: Formation mode observe
- **WHEN** 请求完成时 `memory_formation_mode=observe`
- **THEN** 系统可生成候选和 policy decisions 供调试，但 MUST NOT 执行 Memory Item/Revision/Provider 写入

#### Scenario: Formation mode enforced
- **WHEN** 请求完成时 `memory_formation_mode=enforced`
- **THEN** accepted lifecycle decisions 写入 canonical ledger，并通过 index operation 同步至 mem0 adapter

### Requirement: Formation 决策必须保留请求时策略证据
系统 SHALL 在 Turn 完成或 Outbox 事件中持久化形成资格与抑制 reason code，使后续重启、配置变化和对账修复不会改变请求原本的 private/temporary/off 决策。

#### Scenario: 服务在 Outbox 消费前重启
- **WHEN** completed Turn 已提交但 consumer 尚未处理事件时服务重启
- **THEN** consumer 根据持久化策略证据恢复处理，而不是依赖丢失的进程内状态

#### Scenario: Formation 从 off 改为 enforced
- **WHEN** 一个在 off 模式完成的历史 Turn 在配置开启后被对账
- **THEN** 系统保留原 skipped 决策，不自动追溯写入长期记忆
