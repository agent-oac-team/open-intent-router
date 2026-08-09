## ADDED Requirements

### Requirement: Registry stores generic Agent Definitions
The system SHALL model each registered capability as a generic Agent Definition with identity, display metadata, capabilities, triggers, access policy, schemas, invocation configuration, UI handoff configuration, priority, source, and metadata.

#### Scenario: Generic Agent Definition is persisted
- **WHEN** an admin creates an Agent Definition with required generic fields
- **THEN** the system persists it without requiring OAC, Feishu, Coze, banking, `bot_id`, or `route_path` fields

#### Scenario: Provider-specific data is supplied
- **WHEN** an Agent requires provider-specific identifiers or settings
- **THEN** those values are stored under `invocation.config`, `provider_config`, or `metadata`, not as core top-level fields

### Requirement: Registry supports full CRUD
The system SHALL provide authenticated APIs to create, read, update, enable or disable, delete, and reload Agent definitions.

#### Scenario: Admin creates Agent
- **WHEN** an authenticated admin submits a valid Agent Definition
- **THEN** the system stores the Agent and makes it available for candidate selection when enabled

#### Scenario: Admin disables Agent
- **WHEN** an authenticated admin disables an Agent
- **THEN** the Agent remains stored but does not appear in available or routing candidate lists

#### Scenario: Admin deletes Agent
- **WHEN** an authenticated admin deletes an Agent
- **THEN** the Agent is removed from the active Registry source according to backend capabilities

### Requirement: Registry supports database and file sources
The system SHALL support `database`, `file`, and `hybrid` Registry backends.

#### Scenario: Database backend is configured
- **WHEN** `REGISTRY_BACKEND=database` and the database is available
- **THEN** the system loads Agent Definitions from the database

#### Scenario: File backend is configured
- **WHEN** `REGISTRY_BACKEND=file` and a valid YAML or JSON registry file exists
- **THEN** the system loads Agent Definitions from the file

#### Scenario: Hybrid backend falls back to file
- **WHEN** `REGISTRY_BACKEND=hybrid` and the database is unavailable
- **THEN** the system loads Agent Definitions from the configured file and marks readiness as `degraded`

### Requirement: Registry avoids unsafe implicit merges
The system SHALL avoid implicitly enabling file Agents when the database source is available unless fallback-on-empty or merge behavior is explicitly configured.

#### Scenario: Database is available but empty
- **WHEN** hybrid mode is configured, the database is reachable, and the database contains no Agents
- **THEN** the system does not load file Agents unless the explicit fallback-on-empty setting is enabled

#### Scenario: Duplicate Agent IDs exist in one file
- **WHEN** a file registry contains duplicate `agent_id` values
- **THEN** the system rejects the file registry and reports a configuration error

### Requirement: Registry exposes sanitized public views
The system SHALL return sanitized Agent views to non-admin callers and LLM prompts without secrets, internal URLs, auth headers, or sensitive provider configuration.

#### Scenario: Public Agent list is requested
- **WHEN** a non-admin caller requests available Agents
- **THEN** the response includes routing-relevant Agent summaries and omits sensitive invocation configuration

#### Scenario: LLM prompt candidates are built
- **WHEN** the router constructs candidate Agent data for the LLM
- **THEN** the prompt includes only safe identity, description, capabilities, triggers, domain, and required input summaries
