## ADDED Requirements

### Requirement: Backend persisted state is authoritative
The system SHALL treat persisted Plan, Event, Run, Result, message, and permission state as authoritative over equivalent Host or frontend assertions.

#### Scenario: Frontend active plan conflicts with repository plan
- **WHEN** request context claims a Plan step is running but the persisted Plan records that step as completed
- **THEN** the persisted Plan state is used for context assembly
- **AND** the conflict is recorded in Context Trace

#### Scenario: Host context supplements missing state
- **WHEN** Host context provides a page or interaction signal that has no conflicting backend state
- **THEN** the system may include it as a lower-authority Context Item subject to policy and budget

### Requirement: Context visibility is consumer-specific
Every Context Candidate SHALL declare or derive the consumers and purposes allowed to receive it.

#### Scenario: Router-only diagnostic state
- **WHEN** a Context Item is visible only to the Router or Debug consumer
- **THEN** it is not included in a target Agent's execution projection

#### Scenario: Agent-private history
- **WHEN** Agent history belongs to one Agent session
- **THEN** it is not exposed to another Agent unless an explicit policy permits a derived or referenced form

### Requirement: Permission, sensitivity, and freshness are enforced before ranking
The system SHALL apply permission, subject isolation, source policy, sensitivity handling, deletion/disable state, and expiration before relevance ranking and budget selection.

#### Scenario: Knowledge source is denied
- **WHEN** a Knowledge source is not accessible to the current user, tenant, subject, caller, or purpose
- **THEN** its content is excluded before budget selection
- **AND** Trace records a denied outcome without exposing the denied content

#### Scenario: Memory is expired
- **WHEN** a recalled Memory Item has passed its expiration time
- **THEN** it is not eligible for Router or Agent projection

#### Scenario: Context contains a secret-like field
- **WHEN** candidate content or metadata contains a field covered by redaction policy
- **THEN** consumer projections and persisted traces use the redacted representation

### Requirement: Duplicate context is removed before budget selection
The system SHALL deduplicate equivalent Context Candidates by stable source reference, domain identifier, dedupe key, or a safe normalized hash before spending consumer budget.

#### Scenario: Result and event refer to the same Agent output
- **WHEN** a recent Result and an Agent Event reference the same execution output
- **THEN** the Pipeline selects one canonical representation or complementary non-duplicated fields
- **AND** Trace records the duplicate decision

#### Scenario: Recalled memory repeats recent user input
- **WHEN** a Memory Item contains the same fact explicitly stated in the current input
- **THEN** the current input remains the canonical current-turn representation
- **AND** the duplicate Memory content does not consume additional budget

### Requirement: Context conflicts follow authority and turn semantics
The system SHALL resolve conflicts using fact type, authority, freshness, and current-turn semantics and SHALL retain an auditable conflict outcome.

#### Scenario: Current input overrides a long-term preference for one turn
- **WHEN** recalled Memory says the user prefers Chinese but the current input explicitly requests English
- **THEN** the Router and Agent projections follow the current English request
- **AND** Trace records a current-turn override
- **AND** the Pipeline does not update or delete the long-term Memory

#### Scenario: High-risk conflict cannot be resolved
- **WHEN** two eligible authoritative sources contain a high-risk conflict that deterministic policy cannot resolve
- **THEN** the Pipeline records `unresolved_conflict`
- **AND** the Router receives a bounded conflict signal that can support clarification instead of a silently merged fact

### Requirement: Host context is not permission evidence
Host or frontend context SHALL NOT grant access to an Agent, Memory subject, Knowledge source, Result, Event, Plan, or Artifact.

#### Scenario: Frontend claims an unavailable Agent
- **WHEN** frontend context identifies an Agent that is outside the access-filtered candidate set
- **THEN** that assertion does not make the Agent eligible for routing or context access
