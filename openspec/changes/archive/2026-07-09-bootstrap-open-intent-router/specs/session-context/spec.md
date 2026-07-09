## ADDED Requirements

### Requirement: Session history is stored generically
The system SHALL store chat messages by `session_id`, source, participant role, optional `agent_id`, optional `agent_session_id`, content, metadata, and timestamp.

#### Scenario: Host chat message is recorded
- **WHEN** a route request includes a user text input from the host chat
- **THEN** the system records the user message under source `host_chat`

#### Scenario: Agent chat message is recorded
- **WHEN** an Agent event or invocation result contains an Agent-visible message
- **THEN** the system records the message under source `agent_chat` or `agent_event` with Agent identifiers

### Requirement: Route context is reconstructed from bounded history
The system SHALL build route context from bounded host history, Agent history, recent events, recent results, active plans, frontend context, and artifact references.

#### Scenario: Route context includes recent history
- **WHEN** a route request is processed for an existing session
- **THEN** the context builder includes no more than the configured maximum host and Agent history messages

#### Scenario: Route context includes recent results
- **WHEN** prior Agent results exist in the same session
- **THEN** the context builder exposes recent result summaries and artifact references for follow-up routing

### Requirement: Current Agent continuity is supported
The system SHALL use `current_agent` context and Agent-specific history to distinguish new tasks, continuation requests, Agent switches, and exits.

#### Scenario: User continues current Agent
- **WHEN** a user message is semantically a continuation and `current_agent` is present
- **THEN** routing can return `continue_agent` for that current Agent

#### Scenario: User switches Agents
- **WHEN** a user message requests a different capability than the current Agent provides
- **THEN** routing can return a switch relation and target a different candidate Agent

### Requirement: Artifact references use a normalized object form
The system SHALL normalize artifact references into objects with `artifact_id`, `type`, `uri`, optional title, and metadata while accepting legacy string references at API boundaries.

#### Scenario: Object artifact reference is supplied
- **WHEN** a request includes artifact references as objects
- **THEN** the system stores and passes those objects through context unchanged except for validation

#### Scenario: String artifact reference is supplied
- **WHEN** a request includes artifact references as strings
- **THEN** the system converts them to normalized artifact reference objects internally

### Requirement: MVP does not depend on session state
The system SHALL NOT require persisted `session_state` to route, invoke, or progress plans in MVP.

#### Scenario: No session state exists
- **WHEN** a session has messages, events, results, or plans but no session state row
- **THEN** routing and invocation still operate from reconstructed context
