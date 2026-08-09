## ADDED Requirements

### Requirement: Router performs governed route-stage knowledge retrieval
The system SHALL allow the Router to perform knowledge retrieval for `route_decision` through an explicit, deterministic, and permission-governed policy.

#### Scenario: Router knowledge policy is disabled
- **WHEN** route-stage Knowledge retrieval is not enabled or applicable
- **THEN** the Router does not call the Knowledge Provider for that request

#### Scenario: Router knowledge returns reliable evidence
- **WHEN** route-stage policy enables Knowledge retrieval and accessible reliable evidence is returned
- **THEN** the evidence enters Context Pipeline as governed Candidates
- **AND** selected evidence may support route selection or the existing `reply + assistant_message` response

#### Scenario: Router knowledge has no accessible evidence
- **WHEN** retrieval returns no evidence or all sources are denied
- **THEN** Router context records an empty or denied outcome
- **AND** the Router does not invent a definitive knowledge answer

#### Scenario: Direct reply preserves public schema
- **WHEN** selected Knowledge evidence supports a direct Router reply
- **THEN** the response uses existing `decision.action=reply` and `assistant_message`
- **AND** evidence remains in existing route evidence/Trace fields without requiring a new user Citation field

#### Scenario: Knowledge applicability does not use an extra classifier LLM
- **WHEN** the system decides whether route-stage Knowledge retrieval applies
- **THEN** it uses configured policy, request source, deterministic rules, or Provider applicability
- **AND** it does not make an additional LLM call solely for that decision

## MODIFIED Requirements

### Requirement: Knowledge retrieval degrades safely
The system SHALL keep normal routing and invocation available when Router-stage or Agent-stage Knowledge retrieval fails or times out, unless a future explicit required policy applies.

#### Scenario: Agent knowledge search times out
- **WHEN** Agent execution Knowledge retrieval exceeds its configured timeout
- **THEN** invocation continues with `knowledge_context.status=timeout`
- **AND** Context Trace records the timeout

#### Scenario: Router knowledge search times out
- **WHEN** route-stage Knowledge retrieval exceeds its configured timeout
- **THEN** routing continues with a bounded timeout/no-evidence context outcome
- **AND** the Router remains constrained from inventing unsupported definitive evidence

#### Scenario: Knowledge provider fails
- **WHEN** a Knowledge Provider raises an error for Router or Agent purpose
- **THEN** the system records the Provider error and returns a degraded context result rather than failing the route or invocation by default
