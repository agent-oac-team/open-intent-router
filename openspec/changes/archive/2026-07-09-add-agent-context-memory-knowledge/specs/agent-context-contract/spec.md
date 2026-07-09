## ADDED Requirements

### Requirement: Agent Definition declares context needs
The system SHALL allow each Agent Definition to declare memory and knowledge context requirements in a generic `context` section.

#### Scenario: Agent declares memory prefetch
- **WHEN** an Agent Definition includes `context.memory.mode=prefetch`
- **THEN** the router or Invoker treats memory recall as part of the governed invocation context for that Agent

#### Scenario: Agent leaves knowledge disabled
- **WHEN** an Agent Definition omits `context.knowledge` or sets `context.knowledge.mode=disabled`
- **THEN** the router and Invoker do not prefetch knowledge for that Agent

#### Scenario: Agent declares knowledge prefetch
- **WHEN** an Agent Definition includes `context.knowledge.mode=prefetch`
- **THEN** the router or Invoker attempts knowledge retrieval according to the configured source IDs, limits, and policy

### Requirement: Agent context config remains generic
The Agent context schema SHALL avoid host-specific business fields and SHALL express memory and knowledge needs through generic modes, scopes, source identifiers, limits, and metadata.

#### Scenario: Third-party Bot uses context config
- **WHEN** an Agent represents a HTTP Bot, Coze Bot, low-code workflow, or local function
- **THEN** the same `context` schema can describe memory and knowledge needs without adding provider-specific fields to core Agent Definition

#### Scenario: Host-specific extension is needed
- **WHEN** a host needs private routing or retrieval metadata
- **THEN** that metadata is stored under generic metadata or extension fields and MUST NOT become a required core context field

### Requirement: Invoked Agents receive deterministic context fields
The system SHALL pass memory and knowledge to target Agents through stable invocation fields rather than exposing internal M5/M6 mechanisms.

#### Scenario: Invocation receives memory context
- **WHEN** memory prefetch succeeds or degrades for an Agent invocation
- **THEN** the invocation input includes `memory_context` with `summary`, `items`, `status`, and truncation metadata

#### Scenario: Invocation receives knowledge context
- **WHEN** knowledge prefetch is enabled for an Agent invocation
- **THEN** the invocation input includes `knowledge_context` with `summary`, `items`, `citations`, `source_ids`, `status`, and truncation metadata

#### Scenario: Agent consumes context without internal mechanism
- **WHEN** an Agent receives `memory_context` or `knowledge_context`
- **THEN** it can consume those fields without knowing whether retrieval used mem0, Milvus, Evidence Providers, or another adapter

### Requirement: Context modes are controlled by platform policy
The system SHALL support platform-controlled context retrieval modes and MUST NOT allow target Agents to freely decide arbitrary memory or knowledge retrieval.

#### Scenario: Prefetch mode runs before invocation
- **WHEN** context mode is `prefetch`
- **THEN** the router or Invoker performs retrieval before calling the target Agent

#### Scenario: Controlled retrieval runs from a fixed workflow node
- **WHEN** context mode is `controlled_retrieval`
- **THEN** retrieval is performed only through a configured workflow node template and not through model-selected arbitrary tool use

#### Scenario: Agent requests undeclared context
- **WHEN** a target Agent attempts to access memory or knowledge beyond its declared context policy
- **THEN** the system denies or ignores that request and records the policy outcome

### Requirement: Context prefetch integrates with Context Pack budget
The system SHALL pass prefetched memory and knowledge through Context Pack budgeting before model-bound or Agent-bound use.

#### Scenario: Prefetched context exceeds budget
- **WHEN** recalled memory or knowledge exceeds configured budget or per-item limits
- **THEN** the system truncates or drops lower-priority items and marks the resulting context as truncated

#### Scenario: Critical invocation fields are preserved
- **WHEN** context prefetch produces no usable memory or knowledge
- **THEN** the invocation still receives stable empty or degraded context fields instead of missing unexpected keys
