# status-inspector-panel Specification

## Purpose
TBD - created by archiving change m1-chat-ui-status-panel. Update Purpose after archive.
## Requirements
### Requirement: Unified status inspector

The visual test UI SHALL provide one right-side status inspector that groups route, plan, context, memory, evidence, and debug state into tabs.

#### Scenario: Inspector tabs are shown

- **WHEN** the test UI is loaded
- **THEN** the right-side status area shows tabs for Route, Plan, Context, Memory, Evidence, and Debug

#### Scenario: Route and Plan are primary tabs

- **WHEN** the status inspector renders its tab order
- **THEN** Route appears before Plan, and Plan appears before Context, Memory, Evidence, and Debug

#### Scenario: No response yet

- **WHEN** no route response has been returned
- **THEN** the inspector shows empty states rather than stale route, plan, or invocation data

### Requirement: Route tab summarizes routing state

The status inspector SHALL show route decision details in the Route tab.

#### Scenario: Route response returned

- **WHEN** a route response is available
- **THEN** the Route tab shows decision action, target Agent, decision status, confidence, reason, message, and candidate Agent IDs when present

#### Scenario: Invocation preview returned

- **WHEN** a route response contains `invocation`
- **THEN** the Route tab or Debug tab exposes the invocation preview without adding it to the chat transcript

#### Scenario: Unsupported or clarify response

- **WHEN** the route decision action is `unsupported` or `clarify`
- **THEN** the Route tab shows the status and reason while the assistant chat bubble still displays the user-visible message

### Requirement: Plan tab owns plan operations

The status inspector SHALL render plan state and plan operations in the Plan tab.

#### Scenario: Plan returned by route

- **WHEN** a route response contains `plan`
- **THEN** the Plan tab shows plan id, status, current step, execution policy, next action, and plan steps

#### Scenario: Plan exists without show_plan action

- **WHEN** a route response contains `plan` and `decision.action` is not `show_plan`
- **THEN** the Plan tab still displays the plan

#### Scenario: Plan operation buttons

- **WHEN** a plan is available
- **THEN** the Plan tab provides refresh, confirm-and-execute, execute or continue, resume, and cancel actions backed by the existing plan APIs

### Requirement: Evidence, Context, and Memory tabs are separated

The status inspector SHALL reserve separate tabs for Evidence, Context, and Memory state.

#### Scenario: Evidence returned

- **WHEN** a route response contains evidence in route context
- **THEN** the Evidence tab displays the evidence payload or summary

#### Scenario: Context Pack not implemented yet

- **WHEN** the current response does not contain Context Pack data
- **THEN** the Context tab shows an explicit empty state without implying an error

#### Scenario: Memory not implemented yet

- **WHEN** the current response does not contain memory recall or memory write data
- **THEN** the Memory tab shows an explicit empty state without implying an error

### Requirement: Debug tab keeps raw diagnostics

The status inspector SHALL keep raw diagnostic payloads and developer-only JSON tools in the Debug tab.

#### Scenario: Raw route response

- **WHEN** a route response is available
- **THEN** the Debug tab can display the full RouteResponse JSON with the JSON block collapsed by default

#### Scenario: Invocation result

- **WHEN** route-and-invoke returns an invocation result
- **THEN** the Debug tab can display the full InvocationResult JSON

#### Scenario: Agent Event submission

- **WHEN** a developer needs to submit an Agent Event JSON payload
- **THEN** the Debug tab provides the existing event JSON editor, submit action, and event response display

#### Scenario: UI handoff result

- **WHEN** an invocation result contains UI handoff output
- **THEN** the Debug tab or Route tab displays the handoff route, params, and input safely for local inspection

