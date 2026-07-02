## ADDED Requirements

### Requirement: Context Pack represents model-bound context
The system SHALL represent context selected for routing or execution as a structured Context Pack.

#### Scenario: Route builds context pack
- **WHEN** a route request is processed
- **THEN** the backend builds a Context Pack before calling the LLM

#### Scenario: Context pack has stable identity
- **WHEN** a Context Pack is created
- **THEN** it includes a stable pack identifier, request identifier, session identifier, budget information, selected items, and usage summary

### Requirement: Context items have common structure
The system SHALL convert each context source into Context Items with common metadata.

#### Scenario: User input item
- **WHEN** the current user input is added to the Context Pack
- **THEN** it is represented as a Context Item with source, scope, content, priority, character count, and token estimate

#### Scenario: History item
- **WHEN** host or Agent chat history is added to the Context Pack
- **THEN** each included message is represented as a Context Item rather than an unstructured metadata blob

#### Scenario: Result item
- **WHEN** recent Agent results are added to the Context Pack
- **THEN** each result is represented as a Context Item with source and artifact references when available

#### Scenario: Evidence item
- **WHEN** Evidence is available for a route
- **THEN** each evidence snippet that can enter the model is represented as a Context Item

### Requirement: Context Pack preserves critical current state
The Context Pack SHALL include critical current-state items required for correct routing.

#### Scenario: Current input preserved
- **WHEN** a Context Pack is built
- **THEN** the current user input is included and MUST NOT be dropped by budget trimming

#### Scenario: Current Agent preserved
- **WHEN** the request includes `current_agent`
- **THEN** the Context Pack includes the current Agent state and MUST NOT drop it before lower-priority history

#### Scenario: Current Plan preserved
- **WHEN** the request or route flow references an active plan
- **THEN** the Context Pack includes relevant plan state and uncompleted step information before lower-priority history

### Requirement: LLM route input uses Context Pack
The LLM route input SHALL expose the Context Pack or a derived Context Pack summary to the prompt builder.

#### Scenario: Prompt receives selected context
- **WHEN** the prompt builder constructs messages for a route request
- **THEN** it can access the Context Pack selected items or summary rather than only raw `RouteContext.metadata`

#### Scenario: Context pack metadata location
- **WHEN** M4 exposes Context Pack data to the route response or prompt builder
- **THEN** the first implementation stores the Context Pack summary under `RouteContext.metadata.context_pack` rather than adding a new top-level route response field

#### Scenario: Legacy metadata compatibility
- **WHEN** older code paths still read `RouteContext.metadata`
- **THEN** the system keeps enough compatible metadata for existing behavior until the migration is complete

### Requirement: Context Pack remains generic
The Context Pack schema SHALL use generic source and scope concepts and MUST NOT introduce host-specific business fields.

#### Scenario: Future memory source
- **WHEN** M5 adds memory recall items
- **THEN** those items can use generic Context Item source and scope fields without adding private business concepts to core schema

#### Scenario: Future evidence source
- **WHEN** M6 adds multiple Evidence Providers
- **THEN** evidence items can use generic source, score, and metadata fields without binding core schema to a specific knowledge system
