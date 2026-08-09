## MODIFIED Requirements

### Requirement: Agent invocation uses a unified input contract
The system SHALL construct Agent invocations with run identity, request identity, session identity, Agent identity, Principal-derived user context, normalized input, artifact references, and route context, and SHALL invoke only an Agent selected from the current trusted request's Candidate Set.

#### Scenario: Explicit invocation is requested
- **WHEN** a caller invokes `POST /api/v1/invoke` and the target Agent is present in the Candidate Set formed at that Direct Invoke entry
- **THEN** the system creates an Agent run and sends the normalized invocation to the already-selected definition's invoker

#### Scenario: Explicit target is unavailable
- **WHEN** Direct Invoke targets a missing, disabled, or unauthorized Agent
- **THEN** the system returns `404 agent_not_available` without creating a Run or Result or calling Context providers or an Invoker

#### Scenario: Route-and-invoke prepares invocation
- **WHEN** routing selects an invokable Agent from its Candidate Set and all required inputs are satisfied
- **THEN** the system reuses that request's selected Agent to create a normalized invocation from the route decision and context without repeating access-policy evaluation

### Requirement: Agent event ingestion is idempotent
The system SHALL support trusted Agent event callbacks and prevent duplicate event IDs from causing repeated state transitions. External Native HTTP callbacks MUST have valid Execution Ticket authority for the exact Delegated Run; internal service calls MAY use their trusted in-process boundary.

#### Scenario: New Ticket-authorized Agent event arrives
- **WHEN** a new event with a unique `event_id` and valid matching Execution Ticket is posted for a known Delegated Run
- **THEN** the system records the event and applies the corresponding run, result, or plan update

#### Scenario: External Event lacks execution authority
- **WHEN** an external Native Event lacks a valid matching Execution Ticket
- **THEN** the system rejects it without creating or updating Event, Run, Result, Plan, Turn, or Outbox state

#### Scenario: Duplicate Agent event arrives
- **WHEN** an authorized event with an already processed `event_id` and identical identity is posted again
- **THEN** the system returns a successful idempotent response without applying duplicate side effects
