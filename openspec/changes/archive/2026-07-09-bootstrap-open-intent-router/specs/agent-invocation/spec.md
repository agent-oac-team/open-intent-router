## ADDED Requirements

### Requirement: Agent invocation uses a unified input contract
The system SHALL construct Agent invocations with run identity, request identity, session identity, Agent identity, user context, normalized input, artifact references, and route context.

#### Scenario: Explicit invocation is requested
- **WHEN** a caller invokes `POST /api/v1/invoke` with a valid Agent ID and input
- **THEN** the system creates an Agent run and sends the normalized invocation to the matching invoker

#### Scenario: Route-and-invoke prepares invocation
- **WHEN** routing selects an invokable Agent and all required inputs are satisfied
- **THEN** the system creates a normalized invocation from the route decision and context

### Requirement: Basic MVP invokers are supported
The system SHALL implement invokers for `http`, `local_function`, `mock`, and `ui_handoff` Agent types.

#### Scenario: HTTP Agent is invoked
- **WHEN** an Agent Definition has type `http` and valid HTTP invocation configuration
- **THEN** the system calls the configured endpoint with timeout handling and records the result

#### Scenario: Local function Agent is invoked
- **WHEN** an Agent Definition has type `local_function` and references a trusted registered function
- **THEN** the system calls that registered function without dynamically importing user-provided paths

#### Scenario: Mock Agent is invoked
- **WHEN** an Agent Definition has type `mock`
- **THEN** the system returns the configured mock response and records a completed run

#### Scenario: UI handoff Agent is invoked
- **WHEN** an Agent Definition has type `ui_handoff`
- **THEN** the system does not execute external code and returns host handoff data as the invocation result

### Requirement: Agent runs and results are persisted
The system SHALL persist each Agent execution attempt as an Agent Run and final or intermediate outputs as Agent Results and Agent Events.

#### Scenario: Agent invocation succeeds
- **WHEN** an invoker completes successfully
- **THEN** the system records run status `completed`, stores the output, stores artifact references, and returns the result

#### Scenario: Agent invocation fails
- **WHEN** an invoker times out, raises an error, or returns invalid output
- **THEN** the system records run status `failed` or `invalid_output` with a structured error

### Requirement: Agent output is schema-validated when schema exists
The system SHALL validate Agent outputs against the Agent Definition output schema when provided.

#### Scenario: Output matches schema
- **WHEN** an Agent returns output that satisfies the output schema
- **THEN** the system records the result as valid

#### Scenario: Output violates schema
- **WHEN** an Agent returns output that violates the output schema
- **THEN** the system records an invalid output status and includes schema validation details in structured error form

### Requirement: Agent event ingestion is idempotent
The system SHALL support Agent event callbacks and prevent duplicate event IDs from causing repeated state transitions.

#### Scenario: New Agent event arrives
- **WHEN** a new event with a unique `event_id` is posted for a known run
- **THEN** the system records the event and applies the corresponding run, result, or plan update

#### Scenario: Duplicate Agent event arrives
- **WHEN** an event with an already processed `event_id` is posted again
- **THEN** the system returns a successful idempotent response without applying duplicate side effects
