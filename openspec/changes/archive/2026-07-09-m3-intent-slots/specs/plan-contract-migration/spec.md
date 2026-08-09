## ADDED Requirements

### Requirement: Plan presence is the primary plan contract
The system SHALL treat the presence of a valid `plan` object as the primary signal that a client can display, inspect, or execute a multi-step plan.

#### Scenario: Plan returned with non-show-plan action
- **WHEN** a route response contains a valid `plan` object and `decision.action` is not `show_plan`
- **THEN** clients display the plan and backend plan APIs can operate on it

#### Scenario: Plan returned by route-and-execute
- **WHEN** route-and-execute returns a route response containing `plan`
- **THEN** the caller can inspect the plan regardless of the value of `decision.action`

### Requirement: show_plan remains compatibility-only
The system SHALL keep `decision.action=show_plan` as a backward-compatible action without requiring new route outputs to use it for every plan.

#### Scenario: Legacy show_plan response
- **WHEN** a route response uses `decision.action=show_plan`
- **THEN** the response MUST contain a valid `plan`

#### Scenario: New multi-intent response
- **WHEN** the router generates or normalizes a new multi-intent plan response
- **THEN** it does not need to set `decision.action=show_plan` when `plan` is present

### Requirement: Multi-intent fallback does not force UI action
The router SHALL avoid rewriting a generated fallback plan into a UI-specific action when a more accurate routing action can be preserved.

#### Scenario: Ordered multi-task fallback plan
- **WHEN** the router detects or receives `context.relation=multi_task` and builds a fallback ordered plan
- **THEN** the normalized response contains `context.relation=multi_task`, a valid `plan`, and an appropriate `execution_policy` without requiring `decision.action=show_plan`

#### Scenario: Fallback plan cannot be built
- **WHEN** a multi-task route cannot provide or build a valid plan
- **THEN** the system returns a structured routing error or clarification response instead of silently invoking a single Agent

### Requirement: Plan collaboration messages stay outside chat source
Plan collaboration prompts SHALL be represented by `next_action` and displayed in the Plan status area, while chat output comes from `assistant_message`.

#### Scenario: Confirmation required
- **WHEN** a plan requires user confirmation
- **THEN** the response includes `next_action.type=confirm_plan` and the chat transcript uses `assistant_message` rather than appending `next_action.message`

#### Scenario: Host-managed plan
- **WHEN** a plan is delegated to the host application
- **THEN** the response includes the appropriate `next_action` and the Plan status area displays the collaboration instruction
