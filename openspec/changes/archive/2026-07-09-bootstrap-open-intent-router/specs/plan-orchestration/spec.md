## ADDED Requirements

### Requirement: Router can create multi-step plans
The system SHALL persist structured plans returned by routing when the decision action is `show_plan`.

#### Scenario: Multi-step route is returned
- **WHEN** routing returns `action=show_plan` with valid plan steps
- **THEN** the system stores the plan with status `pending` and stores each step with dependencies and target Agent IDs

#### Scenario: Invalid plan is returned
- **WHEN** routing returns a plan with missing step IDs, invalid dependencies, or unavailable Agents
- **THEN** the system rejects or safely falls back from the route decision

### Requirement: Plans require confirmation before execution
The system SHALL support confirming, cancelling, and inspecting pending plans before execution.

#### Scenario: User confirms plan
- **WHEN** a pending plan is confirmed
- **THEN** the system marks the plan `running` and makes the first executable step ready for invocation

#### Scenario: User cancels plan
- **WHEN** a pending or running plan is cancelled
- **THEN** the system marks the plan `cancelled` and does not start additional steps

### Requirement: Plan steps progress from Agent events
The system SHALL update plan step status when Agent runs or Agent events report progress, completion, failure, or clarification.

#### Scenario: Step completes
- **WHEN** an Agent event reports successful completion for the current plan step
- **THEN** the system marks the step `completed` and makes dependent steps eligible when their dependencies are satisfied

#### Scenario: Step fails
- **WHEN** an Agent event reports failure for a plan step
- **THEN** the system marks the step `failed` and marks the plan `failed` or `blocked` according to failure policy

### Requirement: Plan outputs can feed dependent steps
The system SHALL allow dependent plan steps to reference prior step outputs and artifact references.

#### Scenario: Dependent step starts after prior result
- **WHEN** a step depends on a completed prior step
- **THEN** the invocation input for the dependent step can include the prior step result or artifact references

#### Scenario: Required prior output is missing
- **WHEN** a dependent step requires a prior output that is unavailable
- **THEN** the system marks the step `blocked` or returns a clarification request instead of invoking the Agent
