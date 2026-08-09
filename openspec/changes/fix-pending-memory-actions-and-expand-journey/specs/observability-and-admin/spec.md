## ADDED Requirements

### Requirement: Request-scoped Formation Trace preserves actionable pending decisions
The system SHALL hydrate each authorized Formation Trace with bounded Decision Events from the same tenant, user, and formation job so that request-, session-, turn-, or run-scoped queries preserve the existing management references required to resolve pending decisions.

#### Scenario: Request filter finds a job whose decision event has no request ID
- **WHEN** an authorized caller queries Memory Debug by request ID and the associated Formation Job has a pending Decision Event carrying the job ID but no request ID
- **THEN** the returned Formation Trace includes the Decision Event's `decision_id` and includes `proposed_operation` only when the existing confirmable operation is UPDATE or DELETE

#### Scenario: Turn filter finds a multi-turn formation job
- **WHEN** an authorized caller queries one source turn that belongs to a Formation Job covering multiple turns
- **THEN** the Formation Trace may include the job-level decisions while the top-level Events collection continues to contain only events matching the caller's explicit filters

#### Scenario: Pending ADD remains non-confirmable
- **WHEN** a pending Decision Event contains an ADD candidate
- **THEN** the trace preserves the safe `decision_id` needed to reject the decision but MUST NOT expose ADD as a confirmable UPDATE or DELETE operation

#### Scenario: Sensitive pending candidate is redacted
- **WHEN** a pending candidate was stored with sensitive content redacted
- **THEN** hydration MUST NOT reconstruct or expose the candidate value, quote, or a confirmable operation that cannot be proven from the safe event projection

#### Scenario: Cross-owner job lookup is attempted
- **WHEN** a caller's tenant or user does not own the Formation Job or Decision Event
- **THEN** the hydration query returns no decision association and MUST NOT reveal the event, decision ID, candidate content, or target existence

#### Scenario: Pending decision has been resolved
- **WHEN** a pending decision has a terminal confirm, reject, or conflict event
- **THEN** a `decision_status=pending` query does not return it as unresolved and a general trace query presents its resolved lifecycle state consistently

### Requirement: Trace support events do not widen debug event filters
The system MUST keep Job-level events loaded for Formation Trace projection separate from the caller-visible top-level Memory Events result.

#### Scenario: Request-scoped debug response hydrates a decision
- **WHEN** the service loads a Decision Event by formation job solely to construct an associated Formation Trace
- **THEN** that event is used to populate the trace but is not added to the top-level Events collection unless it independently matches the caller's event filters

#### Scenario: Association lookup remains bounded
- **WHEN** a debug response contains one or more authorized Formation Traces
- **THEN** the service limits both trace count and supporting events per job and does not perform an unbounded tenant-wide event scan
