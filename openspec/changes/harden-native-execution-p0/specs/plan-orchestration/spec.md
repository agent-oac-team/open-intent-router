## MODIFIED Requirements

### Requirement: Plans require confirmation before execution
The system SHALL support confirming, cancelling, and inspecting pending Plans before execution. Cancellation SHALL stop unstarted work immediately but MUST NOT report an active Run cancelled unless a real downstream control was accepted and later confirmed by trusted execution authority.

#### Scenario: User confirms Plan
- **WHEN** a pending Plan is confirmed by its owner
- **THEN** the system marks the Plan `running` and makes the first dependency-satisfied Step ready for invocation

#### Scenario: User cancels Plan with no active Run
- **WHEN** an owned pending Plan or blocked Plan without an active Run is cancelled
- **THEN** the system marks its unstarted Steps and Plan `cancelled` and does not start additional Steps

#### Scenario: User cancels Plan with unsupported active Run control
- **WHEN** an owned Plan has an active Run whose Runtime has no trusted cancellation channel
- **THEN** the system leaves canonical Run and Plan state unchanged and returns `accepted=false`, `transitioned=false`, `reason_code=control_unsupported`, and the current state version

#### Scenario: Unsupported cancel request is duplicated
- **WHEN** the caller retries the same unsupported cancellation while canonical state is unchanged
- **THEN** the system returns the same structured rejection without dispatching control or changing state
