## MODIFIED Requirements

### Requirement: Context Pack represents model-bound context
The system SHALL represent context selected for routing or execution as a structured Context Pack that contains only eligible items for one explicit purpose and consumer.

#### Scenario: Route builds context pack
- **WHEN** a route request is processed
- **THEN** the backend builds a `route_decision` Context Pack before calling the LLM

#### Scenario: Agent execution builds context pack
- **WHEN** a target Agent is selected or invoked directly
- **THEN** the backend builds or derives an `agent_execution` Context Pack before constructing Agent-bound input

#### Scenario: Context pack has stable identity
- **WHEN** a Context Pack is created
- **THEN** it includes a stable pack identifier, request identifier, session identifier, purpose, consumer, budget information, included items, usage summary, policy version, and Trace reference

#### Scenario: Dropped candidates stay outside the pack
- **WHEN** a Context Candidate is denied, expired, duplicated, overridden, invalid, or dropped by budget
- **THEN** it is excluded from the model-bound Context Pack
- **AND** its bounded diagnostic outcome may be recorded in Context Trace

### Requirement: Context items have common structure
The system SHALL convert each context source into common Context Candidates and selected Context Items with source, scope, content or reference, purpose, consumer visibility, authority, quality, policy, budget, and trace metadata.

#### Scenario: User input item
- **WHEN** the current user input is added to context assembly
- **THEN** it is represented with source, request scope, user role, content, authority, priority, character count, token estimate, and current-turn metadata

#### Scenario: History item
- **WHEN** host or Agent chat history is added to context assembly
- **THEN** each eligible message is represented as a Context Candidate rather than an unstructured metadata blob

#### Scenario: Agent reply history item
- **WHEN** a host-written child Agent reply is added to Agent chat history
- **THEN** it is represented with its role, source, Agent identifier, Agent session identifier, content, timestamps, visibility, authority, and metadata subject to the same governance and budget rules as other items

#### Scenario: Result item
- **WHEN** recent Agent results are added to context assembly
- **THEN** each result is represented with a stable result reference, status, bounded summary, and Artifact references when available

#### Scenario: Event item
- **WHEN** a referenced or recent Agent Event is added to context assembly
- **THEN** it is represented with stable event identity, source, status, Agent/Plan references, bounded payload, and consumer visibility

#### Scenario: Evidence item
- **WHEN** Evidence is available for a route
- **THEN** each evidence snippet that can enter the model is represented with source reference, score or confidence, source policy outcome, purpose, and budget metadata

#### Scenario: Memory or knowledge item
- **WHEN** Memory or Knowledge retrieval returns candidates
- **THEN** each candidate uses the common governance fields while retaining the stable domain fields needed to reconstruct `memory_context` or `knowledge_context`

### Requirement: Context Pack preserves critical current state within hard limits
The Context Pack SHALL prioritize critical current state required for correct routing while remaining within the final consumer hard budget.

#### Scenario: Current input preserved
- **WHEN** a Context Pack is built and the current input fits its configured per-item and total limits
- **THEN** the current input is included and lower-priority context is dropped first

#### Scenario: Current input exceeds its allowed representation
- **WHEN** the current input is too large for the configured hard limits
- **THEN** the system applies deterministic truncation, reference conversion, rejection, or a controlled context-budget error
- **AND** it does not silently exceed the hard budget

#### Scenario: Current Agent preserved
- **WHEN** the request includes `current_agent`
- **THEN** the Context Pack includes a bounded representation of current Agent state before lower-priority history

#### Scenario: Current Plan preserved
- **WHEN** the request or session has an active Plan
- **THEN** the Context Pack includes a bounded representation of relevant Plan state and unfinished steps before lower-priority history

#### Scenario: Critical items exceed the hard budget
- **WHEN** all critical items in their minimum allowed representations still exceed the consumer hard budget
- **THEN** context assembly returns a controlled `context_budget_exhausted` outcome instead of exceeding the limit

### Requirement: LLM route input uses Context Pack
The LLM route input SHALL use a whitelist Context Projection derived from the selected Router Context Pack and MUST NOT directly serialize raw request context, `RouteContext.metadata`, Context Trace, or dropped candidates.

#### Scenario: Prompt receives selected context
- **WHEN** the prompt builder constructs messages for a route request
- **THEN** it receives the Router Context Projection containing only selected, rendered context and required route control fields

#### Scenario: Dropped content is absent from prompt
- **WHEN** a history, frontend, result, evidence, memory, knowledge, event, or structured item is dropped
- **THEN** neither its raw content nor its unbounded structured value appears in the Router prompt

#### Scenario: Context pack metadata location
- **WHEN** compatibility Debug data is exposed to the route response
- **THEN** the first implementation may continue storing a bounded Context Pack summary under `RouteContext.metadata.context_pack`
- **AND** the prompt builder does not treat that metadata as model input

#### Scenario: Legacy metadata compatibility
- **WHEN** older clients read `RouteContext.metadata.context_pack`
- **THEN** the system keeps enough bounded compatible metadata for existing Debug behavior during migration
- **AND** model-bound context remains controlled by Context Projection
