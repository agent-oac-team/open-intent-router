## ADDED Requirements

### Requirement: Backend session state is authoritative for context reconstruction
The system SHALL prefer persisted session messages, Events, Plans, Runs, Results, and Artifact references over equivalent request or Host assertions.

#### Scenario: Host state conflicts with persisted state
- **WHEN** frontend or Host context conflicts with persisted Event, Plan, Run, or Result state
- **THEN** route context uses the persisted state
- **AND** the conflict is recorded in Context Trace

#### Scenario: Host state supplements backend state
- **WHEN** Host context provides a non-conflicting page or interaction signal
- **THEN** it may enter context as a lower-authority, policy-controlled Candidate

## MODIFIED Requirements

### Requirement: Route context is reconstructed from bounded history
The system SHALL reconstruct route context from bounded backend host history, Agent history, referenced/recent Events, recent Results, active Plans, Artifact references, governed retrieval, and lower-authority Host context.

#### Scenario: Route context includes recent history
- **WHEN** a route request is processed for an existing session
- **THEN** the context builder considers no more than the configured maximum host and Agent history messages
- **AND** only selected history reaches Router Projection

#### Scenario: Route context includes recent results
- **WHEN** prior Agent Results exist in the same session
- **THEN** the context builder exposes bounded result summaries and normalized Artifact references for follow-up routing

#### Scenario: Agent event request resolves referenced event
- **WHEN** `source=agent_event` and the request contains `event_id`
- **THEN** the context builder loads the referenced Event from the backend repository
- **AND** considers bounded related recent Events and Results

#### Scenario: Active plan is found by session
- **WHEN** a session has an active Plan and the request omits `plan_id`
- **THEN** the context builder can include the backend active Plan and current unfinished step state

#### Scenario: Host context is budgeted
- **WHEN** the request contains frontend or Host context
- **THEN** it is normalized as Context Candidates and does not bypass Context Pipeline governance or budget

### Requirement: Artifact references use a normalized object form
The system SHALL normalize Artifact references into objects with `artifact_id`, `type`, `uri`, optional title, and metadata while accepting legacy string references at API boundaries and extracting stable references from eligible Results.

#### Scenario: Object artifact reference is supplied
- **WHEN** a request or Result includes Artifact references as objects
- **THEN** the system validates and passes bounded allowed fields through context

#### Scenario: String artifact reference is supplied
- **WHEN** an API boundary receives Artifact references as strings
- **THEN** the system converts them to normalized Artifact reference objects internally

#### Scenario: Result provides artifact reference
- **WHEN** a recent Agent Result contains Artifact references
- **THEN** the context builder creates Artifact Context Candidates with stable Result and Artifact source references

#### Scenario: Artifact reference is ambiguous
- **WHEN** a user expression could refer to multiple eligible recent Artifacts and no deterministic rule resolves it
- **THEN** Router context exposes a bounded ambiguity signal that can produce clarification instead of guessing
