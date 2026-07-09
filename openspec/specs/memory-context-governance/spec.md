# memory-context-governance Specification

## Purpose
TBD - created by archiving change add-agent-context-memory-knowledge. Update Purpose after archive.
## Requirements
### Requirement: Memory recall uses declared scopes
The system SHALL recall memory only from scopes allowed by tenant policy and the target Agent's context declaration.

#### Scenario: Agent declares supported memory scopes
- **WHEN** an Agent Definition requests memory scopes such as `user_preference`, `stable_fact`, `task_memory`, `artifact_reference`, or `session_summary`
- **THEN** memory recall considers only those scopes after tenant, subject, and user policy checks

#### Scenario: Agent omits memory scopes
- **WHEN** memory prefetch is enabled but no scopes are declared
- **THEN** the system uses a conservative default scope set or returns an empty memory context according to configuration

#### Scenario: User lacks access to a memory subject
- **WHEN** recall would return memory for a subject the current user cannot access
- **THEN** the system excludes that memory and records the exclusion in debug or audit metadata

### Requirement: Memory context has structured items and summary
The system SHALL assemble recalled memory into `memory_context` with structured items and a compact summary.

#### Scenario: Memory recall succeeds
- **WHEN** relevant memory is recalled for an Agent invocation
- **THEN** `memory_context.items` contains structured memory items and `memory_context.summary` contains a compact representation suitable for low-code Agents

#### Scenario: Memory recall returns no items
- **WHEN** no relevant memory is recalled
- **THEN** `memory_context` is present with empty `items`, an empty or neutral `summary`, and `status=empty`

#### Scenario: Memory context is truncated
- **WHEN** memory context exceeds Context Pack or invocation limits
- **THEN** lower-priority items are removed or summarized and `memory_context.truncated` is set to true

### Requirement: mem0 provides memory strategy behind an adapter
The system SHALL use mem0 as the default memory strategy engine behind a memory adapter boundary.

#### Scenario: Memory is added through strategy layer
- **WHEN** a conversation, Agent result, or Plan result is eligible for memory extraction
- **THEN** the system sends the eligible content to the mem0-backed adapter for extraction, semantic update, or merge behavior

#### Scenario: Memory is searched through strategy layer
- **WHEN** the system recalls memory for a route or invocation
- **THEN** it calls the memory adapter with user, subject, scope, metadata filters, query, and limit information

#### Scenario: Adapter result is governed by OIR
- **WHEN** mem0 returns candidate memories
- **THEN** open-intent-router still applies tenant enablement, Agent scope, permission, TTL, redaction, and budget rules before exposing them

### Requirement: OIR governs memory lifecycle
The system SHALL own memory lifecycle metadata and policy decisions even when mem0 performs strategy operations.

#### Scenario: Low-risk memory is written automatically
- **WHEN** mem0 extracts a low-risk user preference, confirmed stable fact, or task output reference and OIR policy allows writing
- **THEN** the system stores or confirms the memory without showing a normal chat prompt to the end user

#### Scenario: High-risk memory is extracted
- **WHEN** extracted memory is sensitive, low-confidence, unsupported by policy, or outside the Agent's allowed scope
- **THEN** the system does not expose it as normal long-term memory and records the policy decision for debug or admin review

#### Scenario: Current input conflicts with long-term memory
- **WHEN** current user input conflicts with recalled long-term memory
- **THEN** current input controls the current turn but MUST NOT directly rewrite long-term memory

#### Scenario: Conflict is recorded
- **WHEN** current input conflicts with recalled long-term memory
- **THEN** the system records a session override or memory conflict signal for observability and possible later update

### Requirement: Memory TTL defaults are scope-specific
The system SHALL apply scope-specific TTL defaults and cleanup behavior.

#### Scenario: Task memory expires
- **WHEN** a task memory is created without a custom TTL
- **THEN** it receives a 14-day default expiration policy

#### Scenario: Session summary expires
- **WHEN** a session summary memory is created without a custom TTL
- **THEN** it receives a 14-day default expiration policy

#### Scenario: Artifact reference expires
- **WHEN** an artifact reference memory is created without a custom lifecycle
- **THEN** it receives a 14-day default expiration policy or follows the referenced artifact lifecycle

#### Scenario: User preference is long-lived
- **WHEN** a user preference or stable fact is created without a custom TTL
- **THEN** it is long-lived by default and remains visible to user/admin management and deletion flows

### Requirement: Memory observability is admin/debug visible
The system SHALL expose memory recall and write decisions in debug or admin surfaces without adding normal chat prompts for automatic memory writes.

#### Scenario: Memory is auto-written
- **WHEN** a low-risk memory is written automatically
- **THEN** the write appears in route/run logs or admin/debug views with source, scope, confidence, and policy metadata

#### Scenario: Memory is recalled
- **WHEN** memory contributes to `memory_context`
- **THEN** the recall is traceable through debug metadata or route/run logs

