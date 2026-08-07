## MODIFIED Requirements

### Requirement: Backend executes eligible plan steps
The system SHALL provide a backend Plan Executor that forms one Candidate Set for the current trusted execution request and executes eligible Plan Steps through the existing Agent invocation infrastructure using definitions selected from that set.

#### Scenario: Execute ready Step
- **WHEN** a running Plan has a pending Step whose dependencies are completed and whose Agent belongs to the execution request's Candidate Set
- **THEN** the Plan Executor invokes that selected Agent through `InvocationService`

#### Scenario: Plan Agent is unavailable for this execution request
- **WHEN** any non-terminal Step references an Agent outside the execution request's Candidate Set
- **THEN** the executor returns `404 agent_not_available` before creating a Run or invoking an Agent and leaves the Plan unchanged

#### Scenario: Persist invocation result
- **WHEN** a Plan Step invocation completes
- **THEN** the system persists the Agent Run and Result through the existing invocation repositories

#### Scenario: Mark completed Step
- **WHEN** a Plan Step invocation returns a completed result
- **THEN** the system marks the Step as completed and recalculates the ready-set

### Requirement: Executor respects plan dependencies
The Plan Executor SHALL execute a Step only after all its dependencies have completed successfully and SHALL derive the next Step from the full ready-set rather than assuming Plan array order is dependency order.

#### Scenario: Dependent Step waits
- **WHEN** a pending Step depends on another Step whose status is not completed
- **THEN** the executor does not invoke the dependent Step

#### Scenario: Current pointer is not ready but another Step is ready
- **WHEN** `current_step_id` references a pending Step with incomplete dependencies and another pending Step has all dependencies completed
- **THEN** the executor selects the ready Step and does not mark the Plan completed

#### Scenario: Multiple Steps are ready
- **WHEN** two or more pending Steps have all dependencies completed
- **THEN** the executor selects the first such Step in the Plan's original Step order and executes ready Steps serially

#### Scenario: Invalid dependency graph is supplied
- **WHEN** a Plan has duplicate Step IDs, unknown dependencies, or a dependency cycle
- **THEN** Plan validation rejects it before execution

### Requirement: Executor supports confirmation flow
The system SHALL allow a confirmed Plan to start backend execution without requiring the frontend to invoke each Step manually, and each confirm, execute, confirm-and-execute, or resume request SHALL use a Candidate Set created for that trusted request.

#### Scenario: Confirm and execute
- **WHEN** the client confirms a Plan whose policy allows backend execution
- **THEN** the backend forms one Candidate Set for that request and starts executing the next ready eligible Step

#### Scenario: Previously authorized Plan is executed after permissions change
- **WHEN** a Plan is executed or resumed in a request later than the request that created it
- **THEN** the system uses the later request's Principal and Candidate Set rather than the earlier routing snapshot

#### Scenario: Cancelled Plan
- **WHEN** a Plan is canonically cancelled
- **THEN** the executor MUST NOT invoke any additional Steps for that Plan

### Requirement: Executor reports terminal states
The Plan Executor SHALL mark a Plan completed only when every Step is completed, SHALL fail fast on a non-recoverable Step failure, and MUST NOT synthesize completion when an incomplete Plan has no ready Step.

#### Scenario: Plan completed
- **WHEN** all Plan Steps are completed
- **THEN** the Plan status becomes `completed`, `current_step_id` is cleared, and the response contains no further required action

#### Scenario: Plan failed
- **WHEN** a Step invocation fails and cannot continue automatically
- **THEN** the failed Step and Plan become `failed`, no additional Step is invoked, and never-started Steps remain `pending`

#### Scenario: Plan is waiting for external action
- **WHEN** an incomplete Plan is blocked on user input, UI handoff, or a trusted external Agent Event
- **THEN** the Plan remains blocked with its applicable `next_action` and is not marked completed

#### Scenario: Incomplete Plan has no explainable ready Step
- **WHEN** an incomplete, non-failed, non-blocked Plan has no dependency-satisfied pending Step
- **THEN** the system raises an invariant conflict, preserves canonical state, and does not write `completed`
