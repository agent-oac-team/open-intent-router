## ADDED Requirements

### Requirement: Evidence Provider is optional and pluggable
The system SHALL define an Evidence Provider interface that can be enabled or disabled without affecting core routing availability.

#### Scenario: Evidence Provider is disabled
- **WHEN** no Evidence Provider is configured
- **THEN** the router proceeds with Registry candidates, session context, and LLM routing

#### Scenario: Evidence Provider is enabled
- **WHEN** an Evidence Provider is configured
- **THEN** the router calls it before LLM routing and records returned hints or evidence in route context

### Requirement: Fixed-question mapping runs before LLM routing
The system SHALL allow Evidence Providers to match normalized user questions to fixed-question definitions and return intent hints, candidate Agent IDs, or route overrides.

#### Scenario: Strong fixed-question match occurs
- **WHEN** the user input matches a fixed question configured as a strong route override
- **THEN** the router returns or applies the mapped intent or Agent route without requiring LLM routing

#### Scenario: Weak fixed-question match occurs
- **WHEN** the user input matches a fixed question configured as a weak hint
- **THEN** the router injects the hint and candidate Agent IDs into LLM route context

#### Scenario: No fixed-question match occurs
- **WHEN** the user input does not match any fixed question
- **THEN** the router continues with normal LLM routing

### Requirement: Evidence retrieval can enrich route context
The system SHALL allow Evidence Providers to return retrieved evidence snippets with source metadata for routing or direct reply support.

#### Scenario: Evidence is returned
- **WHEN** an Evidence Provider returns relevant evidence
- **THEN** the router includes safe evidence summaries and source metadata in route context and route logs

#### Scenario: Evidence Provider fails
- **WHEN** an Evidence Provider raises an error or times out
- **THEN** the system records the provider error and continues routing unless the provider is configured as required

### Requirement: Evidence Provider does not own core Registry mutation
The system SHALL keep fixed-question and evidence data separate from Agent Registry CRUD unless explicitly imported through a defined adapter.

#### Scenario: Fixed question maps to Agent
- **WHEN** a fixed question maps to an Agent ID
- **THEN** the mapped Agent still must exist and pass Registry access filtering before invocation
