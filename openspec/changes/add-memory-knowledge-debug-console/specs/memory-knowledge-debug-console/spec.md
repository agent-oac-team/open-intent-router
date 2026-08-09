## ADDED Requirements

### Requirement: Debug console exposes read-only memory management data
The frontend SHALL provide a read-only memory debug management view backed by `/api/v1/memories/debug`.

#### Scenario: Load memory debug state
- **WHEN** the user opens or refreshes the memory debug management view
- **THEN** the frontend calls `/api/v1/memories/debug` and displays memory items, memory events, and non-sensitive metadata returned by the API

#### Scenario: Filter memory debug state
- **WHEN** the user enters `user_id`, `tenant_id`, `agent_id`, `scopes`, or `limit` filters
- **THEN** the frontend sends those filters to `/api/v1/memories/debug` and refreshes the displayed memory items and events

#### Scenario: Memory debug secrets are not shown
- **WHEN** memory debug metadata includes provider or mem0 status fields
- **THEN** the frontend displays non-sensitive status, provider, collection, and error summary fields only, and MUST NOT display API keys, database passwords, or Milvus tokens

### Requirement: Debug console exposes read-only knowledge management data
The frontend SHALL provide a read-only knowledge debug management view backed by `/api/v1/knowledge/debug`.

#### Scenario: Load knowledge debug state
- **WHEN** the user opens or refreshes the knowledge debug management view
- **THEN** the frontend calls `/api/v1/knowledge/debug` and displays knowledge sources, chunks, retrieval logs, and metadata returned by the API

#### Scenario: Filter knowledge debug state
- **WHEN** the user enters `source_ids`, `caller_type`, `caller_id`, `purpose`, `tenant_id`, or `limit` filters
- **THEN** the frontend sends those filters to `/api/v1/knowledge/debug` and refreshes the displayed sources, chunks, and logs

#### Scenario: Knowledge debug shows canonical storage boundary
- **WHEN** the knowledge debug management view displays chunks and logs
- **THEN** the frontend labels PostgreSQL source/chunk/log data as canonical and Milvus collection data as vector index metadata when present

### Requirement: Debug console uses existing backend contracts
The debug console SHALL implement the P0/P1 UI scope without requiring new backend write, delete, upload, reindex, or durable trace APIs.

#### Scenario: No write controls in read-only scope
- **WHEN** the user views the memory or knowledge debug management view
- **THEN** the frontend does not show edit, delete, upload, merge, or reindex controls as active operations

#### Scenario: API failure is visible
- **WHEN** `/api/v1/memories/debug` or `/api/v1/knowledge/debug` returns an error
- **THEN** the frontend shows a visible failure state for that debug view without clearing the selected conversation turn trace

#### Scenario: Runtime backend status is visible
- **WHEN** runtime config is available
- **THEN** the debug console shows memory enabled/provider status and knowledge enabled/vector backend status near the debug management views
