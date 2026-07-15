## ADDED Requirements

### Requirement: Task memory 只作为 canonical 任务状态投影
系统 SHALL 将 Plan/Run/Result 派生的 `task_memory` 视为带必填 tenant/user ownership 的低权威定位指针，而不是完整任务事实或 Router 功能意图指令。

#### Scenario: 未完成 Plan 跨会话续接
- **WHEN** 新会话输入表达“继续上次任务”且存在用户有权访问的 active Plan projection
- **THEN** governed route context 只在 stored `tenant_id/user_id` 与当前用户匹配时提供 plan ID、status 和 current/next step pointer，Router 不需要新增记忆或续接专用功能意图

#### Scenario: 用户发起无关新任务
- **WHEN** 新会话输入明确请求与 active task memory 无关的功能
- **THEN** 当前输入 authority 高于 task projection，旧任务 MUST NOT 强制续接、改变目标 Agent 或主回复目标

#### Scenario: Agent 准备继续 Plan
- **WHEN** Agent 需要执行 task memory 指向的未完成任务
- **THEN** 系统按 tenant ID、user ID 和 plan ID 回读 canonical Plan/steps，MUST NOT 仅按 memory summary 执行

#### Scenario: 同租户其他用户存在 active Plan
- **WHEN** 当前用户与另一个用户属于同一 tenant，但 task projection owner 不同
- **THEN** 该 task memory MUST NOT 进入当前用户的 route/agent context 或形成/管理流程

#### Scenario: Plan 进入 terminal 状态
- **WHEN** canonical Plan completed 或 cancelled
- **THEN** structured projection 立即更新 terminal status，并按 task TTL/consolidation 退出 active continuity context

## MODIFIED Requirements

### Requirement: mem0 provides memory strategy behind an adapter
The system SHALL use mem0 behind a memory adapter for storing, updating, deleting, and searching OIR-governed canonical memories; OIR formation and lifecycle policy remain outside the provider.

#### Scenario: Memory is added through strategy layer
- **WHEN** an OIR-formed candidate passes policy and produces an ADD operation
- **THEN** the system sends one canonical memory content item and governed metadata to the mem0-backed adapter with inference disabled

#### Scenario: Memory is updated through strategy layer
- **WHEN** an accepted candidate updates an existing logical memory
- **THEN** the system creates the OIR revision first and asks the adapter to update the mapped mem0 memory ID

#### Scenario: Memory is deleted through strategy layer
- **WHEN** an authorized user deletion or deterministic TTL/lifecycle deletion is accepted
- **THEN** the system excludes the canonical item from recall and asks the adapter to delete the mapped provider record with retryable status

#### Scenario: Memory is searched through strategy layer
- **WHEN** the system recalls memory for a route or invocation
- **THEN** it calls the memory adapter with user, subject, scope, metadata filters, query, and limit information

#### Scenario: Adapter result is governed by OIR
- **WHEN** mem0 returns candidate memories
- **THEN** open-intent-router still applies canonical active status, tenant enablement, Agent scope, permission, TTL, redaction, conflict, and budget rules before exposing them

### Requirement: OIR governs memory lifecycle
The system SHALL own formation evidence, memory keys, policy decisions, current projections, revisions, conflicts, TTL, user deletion, and provider index status even when mem0 performs storage and retrieval operations.

#### Scenario: Low-risk memory is written automatically
- **WHEN** the OIR formation model or structured projector produces a user preference, confirmed stable fact, task projection, result reference, or summary with complete and consistent structured semantics, valid evidence, and confidence >= 0.90
- **THEN** deterministic OIR hard rules and structured semantic validation can accept it without a normal chat confirmation prompt and record the lifecycle decision

#### Scenario: Medium-risk memory is formed
- **WHEN** candidate confidence is between 0.70 and 0.90, structured semantic fields are unknown/inconsistent, verifier is unavailable/uncertain, or the candidate conflicts ambiguously with current memory
- **THEN** the system records PENDING/CONFLICT_PENDING and does not change current long-term memory

#### Scenario: High-risk memory is formed
- **WHEN** candidate memory is sensitive, confidence < 0.70, unsupported by evidence/policy, or outside the allowed subject/scope
- **THEN** the system rejects it, does not expose it as normal long-term memory, and records a redacted policy decision

#### Scenario: Current input conflicts with long-term memory
- **WHEN** current user input conflicts with recalled long-term memory
- **THEN** current input controls the current turn but MUST NOT directly rewrite long-term memory

#### Scenario: Conflict is recorded
- **WHEN** current input conflicts with recalled long-term memory
- **THEN** the system records a bounded session override or memory conflict signal for context and formation observability

#### Scenario: Explicit long-term correction is formed later
- **WHEN** a formation batch contains clear user evidence that a long-term value changed and policy accepts UPDATE
- **THEN** the system creates a new revision and supersedes the previous current revision rather than appending a second active conflict

#### Scenario: Model proposes hard delete
- **WHEN** a model proposes DELETE
- **THEN** deterministic policy MUST verify explicit user authority or deterministic lifecycle reason and a unique target before any provider or ledger deletion

### Requirement: Memory TTL defaults are scope-specific
The system SHALL apply scope-specific TTL defaults, exclude expired memory immediately, and physically delete current/revision content plus provider vectors through the lifecycle sweeper.

#### Scenario: Task memory expires
- **WHEN** a task memory is created without a custom TTL
- **THEN** it receives the configured default expiration policy, initially 14 days

#### Scenario: Session summary expires
- **WHEN** a session summary memory is created without a custom TTL
- **THEN** it receives the configured default expiration policy, initially 14 days

#### Scenario: Artifact reference expires
- **WHEN** an artifact reference memory is created without a custom lifecycle
- **THEN** it receives the configured default expiration policy, initially 14 days, or follows the referenced artifact lifecycle

#### Scenario: User preference is long-lived
- **WHEN** a user preference or stable fact is created without a custom TTL
- **THEN** it is long-lived by default and remains visible to authorized user/admin management and deletion flows

#### Scenario: Memory reaches expiration
- **WHEN** canonical `ttl_expires_at` is reached
- **THEN** the memory is immediately excluded from recall and a hard-delete operation removes current/revision content and the mapped mem0 vector

#### Scenario: Provider expiration hides but does not delete
- **WHEN** mem0 hides a record because `expiration_date` passed
- **THEN** OIR still treats physical provider and canonical deletion as incomplete until the lifecycle operation succeeds

### Requirement: Memory observability is admin/debug visible
The system SHALL expose per-turn recall usage and per-job formation/lifecycle decisions in debug or admin surfaces without adding normal chat prompts for automatic writes or persisting unbounded sensitive content.

#### Scenario: Memory is auto-added or updated
- **WHEN** a formation job creates ADD or UPDATE
- **THEN** the operation appears in memory debug/admin views with source turn/event refs, trigger, scope, memory key/ID, revision, confidence, policy reason, and provider/index status

#### Scenario: Memory candidate is rejected, no-op, or pending
- **WHEN** policy produces REJECT、NOOP 或 PENDING
- **THEN** debug trace shows bounded reason and stable refs without displaying secret or regulated original content

#### Scenario: Memory is recalled
- **WHEN** memory contributes to Router or Agent `memory_context`
- **THEN** the selected turn trace identifies the actual included memory ID/revision, consumer, relevance/confidence, source and projection outcome

#### Scenario: Memory is deleted
- **WHEN** user or TTL deletion is pending, retried, completed, or dead-lettered
- **THEN** admin/debug state shows lifecycle/provider status and actor/reason while tombstone events contain no recoverable memory content
