## ADDED Requirements

### Requirement: Plan presence drives plan UI
The route response SHALL treat the presence of a `plan` object as the primary signal that a client can display or inspect a multi-step plan.

#### Scenario: Plan returned without show_plan
- **WHEN** a route response contains a valid `plan` object and `decision.action` is not `show_plan`
- **THEN** clients can still display the plan and the backend treats the response as a plan-capable route

#### Scenario: show_plan compatibility
- **WHEN** a route response uses the existing `decision.action=show_plan`
- **THEN** the backend continues to validate and process the response for backward compatibility

### Requirement: Route response includes next action
The route response SHALL include a `next_action` object when the host application or user must perform the next step.

#### Scenario: Plan confirmation required
- **WHEN** a plan requires confirmation before execution
- **THEN** the response includes `next_action.type=confirm_plan`

#### Scenario: Host UI handoff required
- **WHEN** a plan step requires the host application to open UI
- **THEN** the response includes `next_action.type=open_ui` with routing metadata safe for the client

#### Scenario: No host action required
- **WHEN** the backend can continue execution without host participation
- **THEN** the response includes no `next_action` or includes `next_action.type=none`

### Requirement: Multi-intent output does not require a UI-specific action
The router SHALL be able to represent multi-intent results without relying on a UI-specific `show_plan` action.

#### Scenario: Multi-intent plan route
- **WHEN** the LLM identifies a sequential multi-intent request
- **THEN** the normalized route output includes `context.relation=multi_task`, a valid `plan`, and an execution policy

#### Scenario: Invalid plan route
- **WHEN** a route response claims a multi-task relation but cannot provide or build a valid plan
- **THEN** the system returns a structured routing error or clarification response instead of silently invoking a single Agent

### Requirement: Route-and-execute endpoint
The system SHALL expose an endpoint that can route a user message and execute the resulting eligible plan or single-Agent invocation according to policy.

#### Scenario: Single Agent route and execute
- **WHEN** route-and-execute receives a single-Agent request
- **THEN** it routes and invokes the selected Agent using the existing invocation path

#### Scenario: Multi-Agent route and execute
- **WHEN** route-and-execute receives a multi-intent request and policy allows backend execution
- **THEN** it creates the plan and starts plan execution through the Plan Executor

#### Scenario: Confirmation required
- **WHEN** route-and-execute receives a multi-intent request that requires confirmation
- **THEN** it returns the plan and `next_action.type=confirm_plan` without executing steps
