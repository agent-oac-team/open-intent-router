## ADDED Requirements

### Requirement: Delegated Run cancellation confirmation is idempotent and atomic
The system SHALL implement the Delegated Run `cancel` command as a trusted execution-participant confirmation and SHALL atomically converge the Run, Canonical Turn, optional Plan Step, Agent Event, and Transactional Outbox to `cancelled` after complete identity, state-version, and terminal-state validation.

#### Scenario: Execution participant confirms cancellation
- **WHEN** a valid Ticket-bound cancellation Event matches an active Delegated Run and its expected state version
- **THEN** the system commits the cancelled Run, cancelled Turn, optional cancelled Plan Step, Agent Event, and Outbox event as one canonical transition

#### Scenario: Cancellation Event is retried
- **WHEN** the same cancellation `event_id` with identical identity is submitted again
- **THEN** the system returns the canonical cancelled result without duplicating state transitions or Outbox effects

#### Scenario: Cancellation conflicts with an existing terminal state
- **WHEN** a completed, failed, cancelled, or timed-out Run receives a different cancellation command
- **THEN** the system preserves the existing terminal state and records no partial cancellation side effect

### Requirement: Unsupported execution control does not fabricate cancellation
The system SHALL treat cancellation of an active Run without a declared and reachable downstream control capability as `control_unsupported`, leave the Run and Plan unchanged, and MUST NOT report `cancel_pending` or `cancelled`.

#### Scenario: Active Run has no cancellation channel
- **WHEN** a caller requests cancellation of a Plan with an active Run and the Runtime has no trusted cancellation channel
- **THEN** the system returns HTTP `200` with `accepted=false`, `transitioned=false`, `reason_code=control_unsupported`, and the unchanged Plan state and state version

#### Scenario: Plan has no active execution
- **WHEN** a caller cancels a pending Plan or a blocked Plan with no active Run
- **THEN** the system cancels its unstarted Steps and returns an accepted canonical cancellation

#### Scenario: Unsupported cancellation is retried
- **WHEN** the same unsupported cancellation request is submitted again while canonical state is unchanged
- **THEN** the system returns the same rejection outcome without dispatch attempts or state changes

### Requirement: Delegated Runs converge automatically at their deadline
The system SHALL run a lifecycle-managed deadline sweeper that selects non-terminal Delegated Runs whose `deadline_at` has passed and applies an idempotent timeout command with a deterministic event identity.

#### Scenario: Active Delegated Run passes its deadline
- **WHEN** the sweeper observes a pending, running, or blocked Delegated Run with `deadline_at <= now`
- **THEN** the system atomically converges its Run, Turn, optional Plan Step, timeout Event, and Outbox to `timed_out` without producing a completed Result

#### Scenario: Heartbeat is stale before deadline
- **WHEN** a Delegated Run heartbeat is stale but its deadline has not passed
- **THEN** the system may expose the run through orphan observation but does not timeout, retry, or otherwise change its canonical state

#### Scenario: Sweeper restarts after a timeout commit
- **WHEN** a process restarts and scans a Run whose deterministic timeout Event was already committed
- **THEN** idempotency preserves the prior timeout transition without duplicate Event or Outbox effects

#### Scenario: Terminal Event races with timeout
- **WHEN** a completion, failure, or cancellation Event and the timeout sweeper race on the same state version
- **THEN** exactly one terminal transition wins and the loser re-reads or rejects without overwriting the canonical terminal state

#### Scenario: Application lifecycle stops
- **WHEN** the FastAPI lifespan shuts down
- **THEN** the deadline sweeper stops and awaits its background task without leaving work attached to a closing event loop

### Requirement: Delegated Run implementation conforms to its application port
The concrete Delegated Run application service SHALL implement every command declared by the runtime-checkable `DelegatedRunApplicationPort`, including `cancel` and `timeout`.

#### Scenario: Concrete service is checked against the port
- **WHEN** the configured real `DelegatedRunService` is evaluated with runtime Protocol conformance
- **THEN** it satisfies `DelegatedRunApplicationPort`
