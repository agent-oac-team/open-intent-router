## ADDED Requirements

### Requirement: Execution Trace uses a bounded generic event envelope
OIR SHALL expose one host-neutral Execution Trace event envelope with the twelve approved event families and only allow facts approved for the selected family.

#### Scenario: A canonical turn is projected
- **WHEN** a trusted caller records a `canonical_turn` event for an existing Canonical Turn
- **THEN** the event contains the Turn's stable ownership and correlation fields, a generic event type, stage and status, and bounded facts without any OAC, IRS or Provider-specific Core schema

#### Scenario: A Provider fact is unsafe
- **WHEN** an event includes a raw payload, prompt, credential, authorization value, secret, unredacted error, or a fact key outside the event family's whitelist
- **THEN** the Writer rejects the event before it is persisted

#### Scenario: A legacy Memory decision is read
- **WHEN** a stored schema v1 `memory_decision` contains legacy previous or proposed Memory body fields
- **THEN** OIR can deserialize the historical event but the OAC Snapshot and SSE projections omit those fields, while schema v2 and later writes reject them

#### Scenario: A lifecycle changes
- **WHEN** a Run moves from running to completed or failed
- **THEN** the trace keeps the same generic event family and represents the lifecycle through `stage` and `status`

### Requirement: Execution Trace is append-only and source-idempotent
The Trace Event Writer SHALL append only new source facts and SHALL make `(source, source_event_id, source_version)` idempotent.

#### Scenario: A source callback is replayed
- **WHEN** the same source, source event ID, version and content are recorded again
- **THEN** the Writer returns the original event and does not allocate a new event offset

#### Scenario: A source identity conflicts
- **WHEN** a caller reuses a source identity with different ownership, correlation or content
- **THEN** the Writer rejects the conflicting write and does not alter the original event

#### Scenario: An event is written to PostgreSQL
- **WHEN** the database storage backend records an event
- **THEN** PostgreSQL assigns a globally monotonic event offset and the event is stored in the single `execution_trace_events` table

### Requirement: Snapshot provides an owned, ordered Trace watermark
OIR SHALL return a Snapshot only for the requested owner and shall include ordered events, Trace Completeness and the highest event offset it covers.

#### Scenario: Owner reads a current turn
- **WHEN** the authenticated owner requests a Snapshot using an existing session and Canonical Turn
- **THEN** events are returned in ascending event offset order with a watermark that covers every returned event

#### Scenario: Different user requests the same turn
- **WHEN** another user or tenant requests the Snapshot
- **THEN** the request is denied without disclosing event existence or content

#### Scenario: OAC session no longer exists
- **WHEN** OAC Go cannot verify that the requested OAC Session still belongs to the authenticated user
- **THEN** its proxy denies the Snapshot and does not call or return the Trace service

### Requirement: SSE resumes after the Snapshot watermark
OIR SHALL stream only owned events whose global offset is greater than the effective Snapshot watermark or standard `Last-Event-ID` cursor.

#### Scenario: Client subscribes after Snapshot
- **WHEN** a client opens SSE with the Snapshot watermark
- **THEN** the stream emits only events whose offsets are greater than that watermark and each SSE event ID equals its event offset

#### Scenario: Client reconnects with a prior cursor
- **WHEN** a client reconnects with a valid `Last-Event-ID`
- **THEN** the stream resumes after that offset in ascending order without emitting already acknowledged events

#### Scenario: Client presents a cursor for another Trace
- **WHEN** a client presents any cursor but fails the current owner/session/turn authorization
- **THEN** the stream is denied and the cursor does not grant access

### Requirement: Trace failure does not change canonical business outcomes
Trace projection failures SHALL not roll back or reclassify successfully committed Canonical business facts, but their absence must remain visible.

#### Scenario: Trace Writer fails after a business fact commits
- **WHEN** a Trace write fails while a Canonical Turn, Run, Result, UI Handoff or Memory fact has otherwise succeeded
- **THEN** the business fact keeps its real outcome and the current response reports incomplete Trace Completeness

#### Scenario: Viewer opens an incomplete Trace
- **WHEN** the Snapshot or SSE scope has a known Trace write gap
- **THEN** it reports `incomplete` with a safe reason code and does not render the trace as a complete normal timeline

#### Scenario: A missing source event is repaired
- **WHEN** the original source fact is available again
- **THEN** it is written through the same Writer using its original source identity and duplicate repair calls remain idempotent

### Requirement: Recovered state is distinguishable from real-time events
OIR MAY create a current-state recovery projection from Canonical Data, but SHALL label it explicitly and shall not fabricate a missing sequence.

#### Scenario: Snapshot has an irrecoverable real-time gap
- **WHEN** the service can derive a current terminal state from Canonical Data
- **THEN** it returns a recovered snapshot marker separate from original event offsets and keeps Trace Completeness incomplete

#### Scenario: No authoritative terminal state exists
- **WHEN** a missing trace cannot be safely reconstructed from Canonical Data
- **THEN** the service reports the gap without inventing a stage or intermediate event

### Requirement: OAC Adapter remains a host boundary
OAC-specific Session ownership, page semantics and Provider mappings SHALL remain outside OIR Core and use application ports.

#### Scenario: OAC queries a Trace
- **WHEN** an OAC Go proxy forwards an owned runtime-observation request with current-only V2 identity
- **THEN** the Host Adapter maps identity to generic ownership and calls the Execution Trace application port without direct Core repository access

#### Scenario: A page handoff completes
- **WHEN** OAC global routing confirms a target path is open
- **THEN** the Adapter records a generic `ui_handoff` event with a bounded host-provided fact and Core does not treat completion as page-internal business completion

### Requirement: Runtime observation is read-only except existing governed decisions
The Trace APIs SHALL not offer execution controls or direct Trace mutation.

#### Scenario: Viewer opens runtime observation
- **WHEN** an authenticated user views an owned Trace
- **THEN** the API only returns Snapshot/SSE observation data and does not expose pause, terminate, rerun, reroute or Trace-edit commands

#### Scenario: A Memory confirmation is available
- **WHEN** an existing governed Memory decision requires confirmation or rejection
- **THEN** the UI invokes the existing authoritative Memory service and a later Trace fact reflects the real outcome rather than mutating an existing Trace event
