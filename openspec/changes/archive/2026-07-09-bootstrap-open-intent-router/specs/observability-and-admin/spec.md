## ADDED Requirements

### Requirement: Health and readiness expose runtime state
The system SHALL expose health and readiness endpoints that report application health, Registry source status, and degraded fallback state.

#### Scenario: Primary Registry is available
- **WHEN** the database Registry source is configured and reachable
- **THEN** readiness reports status `ok`

#### Scenario: Hybrid Registry uses file fallback
- **WHEN** hybrid mode falls back to the local file Registry
- **THEN** readiness reports status `degraded` and identifies the active source as file fallback

#### Scenario: No Registry source is usable
- **WHEN** neither the configured primary nor fallback Registry source can be loaded
- **THEN** readiness reports status `error`

### Requirement: Route logs are recorded
The system SHALL record route logs containing request identifiers, candidate Agent IDs, safe context summaries, LLM output, validation outcomes, evidence hints, and errors.

#### Scenario: Route succeeds
- **WHEN** a route request completes successfully
- **THEN** the system stores a route log with decision, candidate IDs, and timing information

#### Scenario: Route validation fails
- **WHEN** LLM output fails schema or invariant validation
- **THEN** the system stores a route log with validation error details and the fallback decision

### Requirement: Run logs are recorded
The system SHALL record Agent run status transitions, invoker type, latency, structured errors, and result references.

#### Scenario: Invocation completes
- **WHEN** an Agent invocation completes
- **THEN** the system records status, latency, invoker type, and result reference

#### Scenario: Invocation errors
- **WHEN** an Agent invocation fails
- **THEN** the system records structured error code, message, and safe diagnostic metadata

### Requirement: Admin APIs require authentication
The system SHALL require admin authentication for Registry mutation, Registry reload, and other management APIs.

#### Scenario: Admin token is valid
- **WHEN** a management request includes a valid admin token
- **THEN** the system processes the request

#### Scenario: Admin token is missing or invalid
- **WHEN** a management request omits the token or provides an invalid token
- **THEN** the system rejects the request with an authentication error

### Requirement: Sensitive data is redacted
The system SHALL redact API keys, authorization headers, secrets, and sensitive invocation configuration from public APIs and logs.

#### Scenario: Agent uses HTTP auth header
- **WHEN** an Agent Definition includes an HTTP authorization header in invocation config
- **THEN** public Agent views and logs omit or redact that header value

#### Scenario: LLM provider key is configured
- **WHEN** route logs are written during LLM routing
- **THEN** provider API keys are never persisted in route logs
