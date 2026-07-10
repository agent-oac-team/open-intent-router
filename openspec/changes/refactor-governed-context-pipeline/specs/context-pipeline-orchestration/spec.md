## ADDED Requirements

### Requirement: Context Pipeline assembles context by purpose and consumer
The system SHALL assemble governed context through one Context Pipeline that distinguishes route-decision and Agent-execution purposes and binds every Context Pack to an explicit consumer.

#### Scenario: Router assembles route-decision context
- **WHEN** a route request is processed before the Router LLM call
- **THEN** the system assembles a Context Pack with purpose `route_decision` for the Router consumer
- **AND** the Router receives a Context Projection derived from that Pack

#### Scenario: Agent assembles execution context
- **WHEN** routing selects a target Agent or a caller invokes an Agent directly
- **THEN** the system assembles or derives a Context Pack with purpose `agent_execution` for that Agent
- **AND** the Agent input is projected from that Agent-specific Pack

### Requirement: Context Pipeline separates candidates, packs, projections, and traces
The system SHALL represent ungoverned candidates, selected model-bound items, consumer input, and diagnostic decisions as separate internal objects.

#### Scenario: Provider output becomes a candidate
- **WHEN** a Context Provider returns history, state, memory, knowledge, evidence, result, event, plan, or artifact information
- **THEN** the output is normalized as a Context Candidate before it can be selected

#### Scenario: Selected items become a pack
- **WHEN** authority, permission, freshness, conflict, deduplication, and budget processing completes
- **THEN** only eligible included items are placed in the consumer Context Pack

#### Scenario: Trace remains outside consumer input
- **WHEN** the system records dropped items, Provider errors, conflict decisions, or budget decisions
- **THEN** those diagnostics are stored in Context Trace data
- **AND** they are not included in the Router or Agent Context Projection unless separately represented as an eligible consumer fact

### Requirement: Context Providers cannot bypass governance
Every Context Provider SHALL return candidates and metadata to the Context Pipeline and MUST NOT directly append ungoverned content to Router prompts or Agent invocation input.

#### Scenario: Repository Provider returns recent events
- **WHEN** a Repository Provider loads recent Agent Events
- **THEN** it returns bounded Context Candidates with source references
- **AND** the Pipeline applies policy and budget before any event content reaches a consumer

#### Scenario: Retrieval Provider returns knowledge
- **WHEN** a Knowledge Provider returns evidence items
- **THEN** those items enter the same permission, deduplication, budget, projection, and trace stages as other candidates

### Requirement: Context assembly reuses request-scoped Provider results
The system SHALL support request-scoped reuse of equivalent Provider results while reapplying consumer-specific visibility, policy, and budget.

#### Scenario: Router knowledge can be reused for an Agent
- **WHEN** Router-stage knowledge retrieval returned candidates and the selected Agent is permitted to use equivalent sources for the same request
- **THEN** Agent context assembly reuses the request-scoped candidate result instead of executing an equivalent retrieval again
- **AND** it reapplies the Agent's source policy, visibility, and budget before projection

#### Scenario: Direct invoke has an independent assembly session
- **WHEN** an Agent is invoked without a preceding route request
- **THEN** the system creates an independent request-scoped context assembly session

#### Scenario: Context is not reused across requests
- **WHEN** two different requests have the same query text
- **THEN** the Pipeline does not reuse user or tenant context across those requests through this request-scoped mechanism

### Requirement: Context selection does not require an extra LLM call
The system SHALL use deterministic code, policy, Provider applicability, and configured ranking for context governance and MUST NOT call an additional LLM solely to classify, summarize, or select context in this change.

#### Scenario: Context exceeds budget
- **WHEN** candidate context exceeds the available budget
- **THEN** the Pipeline applies deterministic truncation, reference placeholders, ranking, and dropping
- **AND** it does not call a summarization LLM

### Requirement: Context Pipeline supports staged rollout
The system SHALL support legacy, observe, and enforced behavior or an equivalent staged rollout that allows context comparison and rollback without a second Router LLM call.

#### Scenario: Observe mode builds new projection
- **WHEN** Context Pipeline mode is `observe`
- **THEN** the system builds and traces the new Context Projection
- **AND** the existing Router input remains authoritative for the model call
- **AND** no second Router LLM call is made for comparison

#### Scenario: Enforced mode uses only projection
- **WHEN** Context Pipeline mode is `enforced`
- **THEN** Router and Agent model-bound context is produced only from the governed Context Projection

#### Scenario: Legacy rollback preserves security boundaries
- **WHEN** operators switch the pipeline back to `legacy`
- **THEN** existing Agent access policy, Knowledge source policy, and Memory subject isolation remain enforced
