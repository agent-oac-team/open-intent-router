## ADDED Requirements

### Requirement: Backend executes eligible plan steps
The system SHALL provide a backend Plan Executor that executes eligible plan steps by using the existing Agent invocation infrastructure.

#### Scenario: Execute current step
- **WHEN** a running plan has a pending current step whose Agent is backend-invokable
- **THEN** the Plan Executor invokes that Agent through `InvocationService`

#### Scenario: Persist invocation result
- **WHEN** a plan step invocation completes
- **THEN** the system persists the Agent run and result through the existing invocation repositories

#### Scenario: Mark completed step
- **WHEN** a plan step invocation returns a completed result
- **THEN** the system marks the step as completed and advances `current_step_id` to the next dependency-satisfied pending step

### Requirement: Executor respects plan dependencies
The Plan Executor SHALL execute a step only after all step dependencies have completed successfully.

#### Scenario: Dependent step waits
- **WHEN** a pending step depends on another step whose status is not completed
- **THEN** the executor does not invoke the dependent step

#### Scenario: Next ready step
- **WHEN** all dependencies of a pending step are completed
- **THEN** the executor may select that step as the next executable step

### Requirement: Executor pauses for host action
The Plan Executor SHALL pause and return a host-facing next action when a step cannot be executed fully in the backend.

#### Scenario: UI handoff step
- **WHEN** the current step references an Agent with `type=ui_handoff`
- **THEN** the executor pauses the plan and returns a `next_action` instructing the host to open the target UI

#### Scenario: Missing input
- **WHEN** the current step cannot be invoked because required input is missing
- **THEN** the executor marks the plan as blocked and returns a `next_action` asking the host to collect input

### Requirement: Executor supports confirmation flow
The system SHALL allow a confirmed plan to start backend execution without requiring the frontend to invoke each step manually.

#### Scenario: Confirm and execute
- **WHEN** the client confirms a plan whose policy allows backend execution
- **THEN** the backend starts executing the current eligible step

#### Scenario: Cancelled plan
- **WHEN** a plan is cancelled
- **THEN** the executor MUST NOT invoke any additional steps for that plan

### Requirement: Executor reports terminal states
The Plan Executor SHALL update the plan to a terminal state when all steps complete or any non-recoverable step fails.

#### Scenario: Plan completed
- **WHEN** all plan steps are completed
- **THEN** the plan status becomes `completed` and the response contains no further required action

#### Scenario: Plan failed
- **WHEN** a step invocation fails and cannot continue automatically
- **THEN** the plan status becomes `failed` or `blocked` and the response includes failure details
