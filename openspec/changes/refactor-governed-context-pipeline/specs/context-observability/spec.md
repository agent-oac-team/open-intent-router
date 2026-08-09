## ADDED Requirements

### Requirement: Model input and diagnostics are separately observable
The system SHALL expose enough data to prove which governed Context Projection was used without making Context Trace or Debug payloads part of model input.

#### Scenario: Prompt projection is identified
- **WHEN** a Router or Agent Context Projection is created
- **THEN** Trace records its purpose, consumer, projection version, bounded summary or hash, and estimated usage

#### Scenario: Debug trace is not prompt content
- **WHEN** Debug data contains dropped item summaries or Provider errors
- **THEN** the Router and Agent prompts do not contain that Debug data unless a separate eligible consumer fact was created

## MODIFIED Requirements

### Requirement: Route Log records Context Pack usage
The system SHALL record a bounded Context Pack and Context Trace summary in Route Log data for each routed request.

#### Scenario: Route succeeds
- **WHEN** a route request completes successfully
- **THEN** the Route Log includes pack/trace identity, purpose, consumer, policy/budget/projection versions, budget values, included/dropped/truncated counts, source distribution, and Projection summary or hash

#### Scenario: Route fails after context selection
- **WHEN** a route request fails after Context Pack construction
- **THEN** the Route Log or error diagnostics retain the available bounded Trace summary without exposing raw credentials or unbounded source text

#### Scenario: Provider degrades
- **WHEN** a Context Provider is skipped, denied, times out, or fails
- **THEN** the Route Log records Provider identity, status, purpose, and bounded error metadata

### Requirement: Debug state explains context selection
The route response or Debug metadata SHALL explain authority, permission, visibility, freshness, deduplication, conflict, budget, and Provider outcomes for Context Candidates without requiring unbounded raw content.

#### Scenario: Included item visible
- **WHEN** a Context Item is included in consumer input
- **THEN** Debug data can show its source reference, purpose, consumer, authority, priority, estimated cost, and selected status

#### Scenario: Host-written chat message visible
- **WHEN** a host-written child Agent reply is selected or dropped as a history Context Candidate
- **THEN** Debug data can show its source, role, Agent identifier, authority, selected status, token estimate, and drop reason without requiring unbounded raw message text

#### Scenario: Dropped item visible
- **WHEN** a Context Candidate is dropped
- **THEN** Debug data can show its stable reference, source, priority, estimated cost, and drop reason

#### Scenario: Truncated item visible
- **WHEN** a Context Item is truncated or reference-projected
- **THEN** Debug data can show the applied transformation without displaying the full original text or structured value

#### Scenario: Conflict visible
- **WHEN** Candidates conflict or one is overridden for the current turn
- **THEN** Debug data shows the conflict key, outcome, and involved source references in a bounded form

### Requirement: Context observability avoids unbounded logging
The system SHALL avoid writing unbounded raw candidates, complete structured values, complete prompts, or sensitive Context Trace data to persistent logs by default.

#### Scenario: Long history item
- **WHEN** a long history Candidate is considered for a route
- **THEN** persistent logs record bounded metadata, source reference, selected status, and transformation reason rather than the entire raw body

#### Scenario: Evidence item with source metadata
- **WHEN** an Evidence Candidate is included
- **THEN** logs can record source identifiers, score, policy outcome, and usage summary without requiring the full source document text

#### Scenario: Sensitive metadata
- **WHEN** Candidate, Provider, Projection, or Trace metadata contains fields covered by redaction rules
- **THEN** persisted and response diagnostics apply redaction before storage or display

#### Scenario: Structured value is large
- **WHEN** a Candidate contains a large structured value
- **THEN** logs store only an allowed bounded projection, source reference, or hash

### Requirement: Context Pack diagnostics support later replay
The Context Trace summary SHALL provide stable metadata for later evaluation, comparison, and replay foundations across policy versions.

#### Scenario: Item identifiers recorded
- **WHEN** Context Candidates are selected, transformed, or dropped
- **THEN** diagnostics include stable item identifiers or source references suitable for comparing route runs

#### Scenario: Budget comparison
- **WHEN** two route logs are compared later
- **THEN** their summaries expose budget values, usage source, purpose, consumer, and projection version

#### Scenario: Policy comparison
- **WHEN** the same request is evaluated under different context policy versions
- **THEN** diagnostics expose policy, budget, and projection versions plus the final Projection hash or bounded summary
