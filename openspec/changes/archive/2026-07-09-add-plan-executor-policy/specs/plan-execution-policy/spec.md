## ADDED Requirements

### Requirement: Plans declare execution policy
The system SHALL associate every routed plan with an execution policy that determines whether the backend returns the plan only, waits for confirmation, executes automatically, or delegates execution to the host application.

#### Scenario: Return-only plan
- **WHEN** routing produces a multi-intent plan with `execution_policy=return_plan_only`
- **THEN** the system persists or returns the plan without invoking any Agent

#### Scenario: Confirmation-gated plan
- **WHEN** routing produces a multi-intent plan with `execution_policy=require_confirmation`
- **THEN** the system returns a `next_action` instructing the client to confirm or cancel the plan

#### Scenario: Auto-executable plan
- **WHEN** routing produces a multi-intent plan with `execution_policy=auto_execute`
- **THEN** the system starts executing eligible backend-invokable steps without requiring the client to invoke each step

#### Scenario: Host-managed plan
- **WHEN** routing produces a multi-intent plan with `execution_policy=host_managed`
- **THEN** the system returns the plan and marks host application execution as the required next action

### Requirement: Execution policy has safe defaults
The system SHALL choose an execution policy from global configuration when the route response does not explicitly provide one.

#### Scenario: Missing policy
- **WHEN** an LLM route response contains a plan but no execution policy
- **THEN** the system applies the configured default execution policy

#### Scenario: Non-local safety
- **WHEN** the application runs outside local development and no explicit auto-execution setting is configured
- **THEN** the default execution policy MUST NOT be `auto_execute`

### Requirement: Agent Registry remains minimal
The system SHALL NOT require new first-class Agent Registry fields to execute MVP plan policies.

#### Scenario: Existing invokable Agent
- **WHEN** a plan step references an Agent whose existing invocation type is `mock`, `http`, or `local_function`
- **THEN** the executor treats the step as backend-invokable without requiring additional registry fields

#### Scenario: Optional metadata override
- **WHEN** an Agent definition includes optional execution metadata
- **THEN** the system may use it as an override without making it mandatory for other Agents
