## ADDED Requirements

### Requirement: Route Log records Context Pack usage
The system SHALL record Context Pack usage summary in Route Log data for each routed request.

#### Scenario: Route succeeds
- **WHEN** a route request completes successfully
- **THEN** the Route Log includes Context Pack usage summary, budget values, included item counts, dropped item counts, and source distribution

#### Scenario: Route fails after context selection
- **WHEN** a route request fails after Context Pack construction
- **THEN** the Route Log or error diagnostics retain available Context Pack summary without exposing raw credentials or unbounded source text

### Requirement: Debug state explains context selection
The route response or debug metadata SHALL expose enough Context Pack information for local diagnosis.

#### Scenario: Included item visible
- **WHEN** a Context Item is included in the model-bound context
- **THEN** Debug data can show its source, priority, token estimate, and selected status

#### Scenario: Host-written chat message visible
- **WHEN** a host-written child Agent reply is selected or dropped as a history Context Item
- **THEN** Debug data can show its source, role, agent identifier, selected status, token estimate, and drop reason without requiring unbounded raw message text in persistent logs

#### Scenario: Dropped item visible
- **WHEN** a Context Item is dropped
- **THEN** Debug data can show its source, priority, token estimate, and drop reason

#### Scenario: Truncated item visible
- **WHEN** a Context Item is truncated
- **THEN** Debug data can show that truncation occurred without requiring the full original text to be displayed

### Requirement: Test UI Context tab displays Context Pack state
The visual test UI SHALL display Context Pack state in the right-side Context tab when available.

#### Scenario: Context Pack available
- **WHEN** the latest route response includes Context Pack debug data
- **THEN** the Context tab shows budget usage, selected item count, dropped item count, and source groups

#### Scenario: Dropped context items
- **WHEN** the Context Pack contains dropped or trimmed items
- **THEN** the Context tab shows their reasons in a developer-readable form

#### Scenario: Context Pack unavailable
- **WHEN** no Context Pack data is available
- **THEN** the Context tab shows an explicit empty state rather than stale data

### Requirement: Context observability avoids unbounded logging
The system SHALL avoid writing unbounded raw context content to persistent logs by default.

#### Scenario: Long history item
- **WHEN** a long history Context Item is considered for a route
- **THEN** persistent logs record summary metadata and selected status rather than the entire raw history body by default

#### Scenario: Evidence item with source metadata
- **WHEN** an Evidence Context Item is included
- **THEN** logs can record source identifiers, score, and usage summary without requiring the full source document text

#### Scenario: Sensitive metadata
- **WHEN** Context Item metadata contains fields already covered by redaction rules
- **THEN** persisted diagnostic data applies existing redaction behavior before storage or display

### Requirement: Context Pack diagnostics support later replay
The Context Pack summary SHALL provide enough stable metadata to support later evaluation and replay modules.

#### Scenario: Item identifiers recorded
- **WHEN** Context Items are selected or dropped
- **THEN** diagnostics include stable item identifiers or source references suitable for comparing route runs

#### Scenario: Budget comparison
- **WHEN** two route logs are compared later
- **THEN** their Context Pack summaries expose budget values and usage source metadata needed for comparison
