## ADDED Requirements

### Requirement: Agent context safely reuses request-scoped retrieval
The system SHALL reuse equivalent request-scoped Router retrieval candidates for Agent execution only when the target Agent, user, tenant, purpose, source policy, and context declaration permit reuse.

#### Scenario: Agent can reuse Router knowledge
- **WHEN** Router retrieval returned a Knowledge Candidate and the target Agent declares and is permitted to use the same source
- **THEN** Agent context assembly reuses that candidate without an equivalent second Provider call
- **AND** reapplies Agent visibility, source policy, and budget

#### Scenario: Agent cannot reuse Router-only context
- **WHEN** a Router Candidate is not visible to the target Agent or is outside the Agent's declared context policy
- **THEN** the Agent execution Context Pack excludes it

## MODIFIED Requirements

### Requirement: Invoked Agents receive deterministic context fields
The system SHALL pass memory and knowledge to target Agents through stable invocation fields generated from an Agent-specific governed Context Projection rather than exposing internal retrieval or Router Debug mechanisms.

#### Scenario: Invocation receives memory context
- **WHEN** memory prefetch succeeds or degrades for an Agent invocation
- **THEN** the Agent Projection includes `memory_context` with `summary`, selected `items`, `status`, errors, and truncation metadata

#### Scenario: Invocation receives knowledge context
- **WHEN** knowledge prefetch is enabled for an Agent invocation
- **THEN** the Agent Projection includes `knowledge_context` with `summary`, selected `items`, citations, source IDs, `status`, errors, and truncation metadata

#### Scenario: Agent consumes context without internal mechanism
- **WHEN** an Agent receives `memory_context` or `knowledge_context`
- **THEN** it can consume those fields without knowing whether retrieval used mem0, Milvus, Evidence Providers, request-scoped reuse, or another adapter

#### Scenario: Route response retains invocation preview
- **WHEN** routing selects an Agent with complete required input
- **THEN** `RouteResponse.invocation.input` remains the stable Agent input preview generated from the Agent Projection

#### Scenario: Debug trace is excluded
- **WHEN** Context Trace contains Router-only decisions, dropped items, or Provider diagnostics
- **THEN** that Trace data is not copied into Agent invocation input

### Requirement: Context prefetch integrates with Context Pack budget
The system SHALL pass prefetched or reused memory and knowledge through the Agent execution Context Pack hard budget before Agent-bound use.

#### Scenario: Prefetched context exceeds budget
- **WHEN** recalled memory or knowledge exceeds configured total, source, or per-item Agent context limits
- **THEN** the system truncates, references, or drops lower-value items before building `memory_context` and `knowledge_context`
- **AND** marks the resulting domain context as truncated when applicable

#### Scenario: Critical invocation fields are preserved
- **WHEN** context prefetch produces no usable memory or knowledge
- **THEN** the invocation still receives stable empty or degraded context fields instead of missing unexpected keys

#### Scenario: Agent projection exceeds hard budget
- **WHEN** the combined Agent input, memory, knowledge, and structured values would exceed the Agent context hard budget
- **THEN** the Projection applies deterministic reduction or returns a controlled context-budget outcome
- **AND** it does not silently exceed the limit
