## ADDED Requirements

### Requirement: Plans have required server-controlled ownership
The system SHALL require every Plan to contain non-empty `user_id` and `tenant_id`, SHALL bind those values from trusted current `UserContext`, and MUST NOT trust ownership values generated or supplied by the Router LLM, Host, or frontend.

#### Scenario: Router creates a Plan
- **WHEN** routing returns a valid Plan draft for a request with trusted user and tenant identity
- **THEN** RouterService injects and overwrites `user_id/tenant_id` before final Plan validation and persistence

#### Scenario: LLM or Host supplies different ownership
- **WHEN** raw Plan data includes `user_id/tenant_id` that differ from trusted current UserContext
- **THEN** the system ignores those values and binds the trusted identity

#### Scenario: Plan creation lacks user or tenant
- **WHEN** a Plan creation/execution request has no trusted user ID or tenant ID
- **THEN** the system rejects Plan creation instead of persisting an unowned or tenant-wide Plan

#### Scenario: Plan is read or mutated
- **WHEN** a caller gets, confirms, cancels, executes, or otherwise mutates a Plan
- **THEN** the service/repository verifies the caller's `tenant_id + user_id` against stored Plan ownership before returning content or changing state

#### Scenario: Another user in the same tenant accesses a Plan
- **WHEN** a different user in the same tenant attempts to read, confirm, cancel, execute, or form task memory for the Plan
- **THEN** the system denies access because the initial scope supports personal Plans only

#### Scenario: Agent event updates a Plan
- **WHEN** an Agent event references an existing Plan
- **THEN** the system resolves the Plan through trusted run/session association, preserves stored ownership, and MUST NOT accept new owner fields from the event

### Requirement: Plan ownership has no legacy compatibility path
The system SHALL treat required Plan ownership as the only supported development contract and SHALL NOT maintain nullable-owner, legacy-unowned, or ownership-backfill behavior.

#### Scenario: Existing development Plan lacks ownership
- **WHEN** local/test data contains a Plan without user or tenant ownership after the new schema is applied
- **THEN** the data is cleared or rejected and MUST NOT be loaded, executed, or projected into task memory

#### Scenario: Database schema is created
- **WHEN** the Plan table schema is initialized for this change
- **THEN** `plans.user_id` and `plans.tenant_id` are both NOT NULL and indexed for authorized lookup

### Requirement: Plan execution retries expose a stable idempotency contract
The system SHALL treat external Agent execution as at-least-once, SHALL renew the canonical Plan claim while an invocation is active, and SHALL provide the same stable execution idempotency key when an expired claim for the same attempt is recovered. HTTP Agents MUST honor the `Idempotency-Key` header and local-function Agents MUST transactionally deduplicate `invocation.context.plan_execution_idempotency_key` before committing external side effects. The claim token alone MUST NOT be described as an exactly-once guarantee.

#### Scenario: Long-running Agent keeps its claim
- **WHEN** an Agent invocation runs longer than the original Plan claim lease
- **THEN** OIR renews the lease while the invocation is active so another executor cannot normally reclaim the same step

#### Scenario: Executor crashes after an external side effect
- **WHEN** the executor loses its claim or crashes before persisting Result after the Agent committed a side effect
- **THEN** a recovered execution uses the same stable idempotency key and the Agent deduplicates the repeated request before applying another side effect

#### Scenario: Blocked step is explicitly resumed
- **WHEN** a blocked step is resumed with new user input after the prior claim was invalidated
- **THEN** OIR creates a new execution attempt and a different idempotency key

## MODIFIED Requirements

### Requirement: Router can create multi-step plans
The system SHALL persist structured plans returned by routing when the decision action is `show_plan`, after validating the Plan structure and binding trusted user/tenant ownership.

#### Scenario: Multi-step route is returned
- **WHEN** routing returns `action=show_plan` with valid plan steps for a request with trusted user and tenant identity
- **THEN** the system stores the plan with status `pending`, required `user_id/tenant_id`, and each step with dependencies and target Agent IDs

#### Scenario: Invalid plan is returned
- **WHEN** routing returns a plan with missing step IDs, invalid dependencies, unavailable Agents, or the request lacks trusted ownership
- **THEN** the system rejects or safely falls back from the route decision and MUST NOT persist the Plan
