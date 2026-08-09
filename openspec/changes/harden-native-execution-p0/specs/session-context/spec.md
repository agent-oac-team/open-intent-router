## ADDED Requirements

### Requirement: Native Session message access enforces Canonical Ownership
The system SHALL bind Native Session message reads and writes to the authenticated Principal's tenant and subject. Body or query owner fields MUST NOT select another Principal's message history.

#### Scenario: Owner reads Session history
- **WHEN** a Principal requests messages for its own Session
- **THEN** the system reads bounded messages using the Principal tenant and subject

#### Scenario: Cross-owner Session is requested
- **WHEN** a Principal requests a Session that only contains messages owned by another subject or tenant
- **THEN** the system returns `404` and does not return foreign messages

#### Scenario: Host app appends a visible message
- **WHEN** a Principal appends a Host or Agent display message to its Session
- **THEN** the system overwrites any submitted owner fields with the Principal tenant and subject before storing the derived message

#### Scenario: Body claims another Session owner
- **WHEN** an append request supplies a different `user_id` or `tenant_id`
- **THEN** the system does not write a message under the claimed foreign owner
