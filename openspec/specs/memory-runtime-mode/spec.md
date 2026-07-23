# memory-runtime-mode Specification

## Purpose
TBD - created by archiving change simplify-memory-runtime-mode. Update Purpose after archive.
## Requirements
### Requirement: Memory behavior has one environment mode
The system SHALL accept `MEMORY_MODE=off|observe|on` as the only environment variable that controls whether Memory recall, automatic formation, and related background behavior are active.

#### Scenario: Valid mode is configured
- **WHEN** the process starts with `MEMORY_MODE` set to `off`, `observe`, or `on`
- **THEN** the system derives one immutable effective Memory runtime policy before starting providers or workers

#### Scenario: Invalid mode is configured
- **WHEN** `MEMORY_MODE` contains any other value
- **THEN** startup fails before external providers or workers are started and reports the allowed modes without exposing secrets

### Requirement: Memory modes have deterministic behavior
The system SHALL derive Recall, Formation, Outbox, worker, maintenance, and route-context behavior from a fixed versioned mode matrix.

#### Scenario: Memory mode is off
- **WHEN** effective `MEMORY_MODE=off`
- **THEN** the system does not recall Memory into business requests or create new Formation jobs, keeps the Turn Outbox audit path deterministic, and continues delete, TTL, index repair, and already-committed operation maintenance

#### Scenario: Memory mode is observe
- **WHEN** effective `MEMORY_MODE=observe`
- **THEN** the system runs bounded Formation observation and records decisions without writing Memory items/revisions/provider indexes and without injecting recalled Memory into business requests

#### Scenario: Memory mode is on
- **WHEN** effective `MEMORY_MODE=on`
- **THEN** Recall, enforced Formation, Turn Outbox consumption, Formation worker/sweeper, index maintenance, and TTL maintenance are active

#### Scenario: Consolidation is not part of initial on mode
- **WHEN** effective `MEMORY_MODE=on` in this policy version
- **THEN** automatic consolidation remains disabled and cannot be enabled through a hidden environment override

### Requirement: Retired behavior variables cannot override the mode
The system MUST NOT accept legacy Recall, Formation, execution, worker, sweeper, maintenance, consolidation, or route-memory environment variables as independent behavior configuration after this change.

#### Scenario: Retired variable remains in deployment environment
- **WHEN** startup detects any retired Memory behavior environment variable
- **THEN** startup fails before external connections and lists the retired variable name plus the `MEMORY_MODE` migration target without logging its value

#### Scenario: Infrastructure settings remain externalized
- **WHEN** a deployment configures database, Provider, model, Embedding, Milvus, credential, timeout, threshold, budget, TTL, lease, retry, or interval parameters
- **THEN** those infrastructure and policy values remain externally configurable but cannot change the effective mode matrix

### Requirement: Host execution isolation is composition-owned
The system SHALL use `live` execution for the generic OIR composition and SHALL allow a Host composition to inject a stricter shadow or rehearsal execution plane without exposing `MEMORY_EXECUTION_MODE` as an environment variable.

#### Scenario: Generic OIR starts
- **WHEN** OIR starts without a Host-specific composition override
- **THEN** its effective Memory execution plane is `live`

#### Scenario: OAC Decision Shadow starts
- **WHEN** the OAC Host composition is configured for Decision Shadow
- **THEN** it injects `decision_shadow`, which prevents Recall injection and Memory write side effects regardless of `MEMORY_MODE`

#### Scenario: OAC State Rehearsal starts
- **WHEN** the OAC Host composition is configured for State Rehearsal
- **THEN** it injects `state_rehearsal` and startup verifies that the rehearsal database and collections are isolated from primary data

### Requirement: Effective Memory policy is observable and redacted
The system SHALL expose the selected mode and derived effective behavior through Runtime Config, capability, health, and startup diagnostics without exposing sensitive configuration.

#### Scenario: Operator reads runtime configuration
- **WHEN** an authorized or safe public runtime endpoint is read
- **THEN** the response identifies `memory_mode`, effective execution plane, Recall/Formation/worker/context states, policy version, and configuration source

#### Scenario: Compatibility booleans are returned
- **WHEN** an existing client reads a temporarily retained low-level status field
- **THEN** the field is a read-only projection of the effective mode and is not reported as an independent configuration source

#### Scenario: Runtime output is serialized
- **WHEN** effective Memory policy is logged or returned
- **THEN** no credential, environment value, database URL, candidate content, or user memory content is included

### Requirement: OAC initial Agent Memory rollout is explicit Registry data
The OAC migration SHALL enable Agent-level Memory only through versioned Registry definitions and MUST NOT add OAC Agent IDs to OIR Core or conditional Adapter logic.

#### Scenario: Approved conversational Agents are migrated
- **WHEN** the initial OAC Memory Registry migration is applied
- **THEN** `strategy_analysis` and `compliance_review` declare `mode=prefetch`, scopes `user_preference` and `stable_fact`, and `max_items=5`

#### Scenario: Other migrated Agents are inspected
- **WHEN** the same Registry migration is applied
- **THEN** the other seven OAC Agents remain `memory.disabled` unless a later audited Registry revision explicitly enables them

#### Scenario: Registry update is audited
- **WHEN** either approved Agent's Memory context changes
- **THEN** the change is recorded through existing Registry revision and audit semantics rather than a hard-coded runtime mapping
