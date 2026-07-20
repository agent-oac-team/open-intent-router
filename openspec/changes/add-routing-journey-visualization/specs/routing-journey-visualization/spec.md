## ADDED Requirements

### Requirement: Default routing journey view

The visual test UI SHALL provide a default routing journey view in the right-side status inspector that explains the selected conversation turn using plain-language stages.

#### Scenario: UI opens without a selected turn
- **WHEN** the visual test UI has no conversation turn to inspect
- **THEN** the journey view shows an explicit empty state and does not display stale data from an earlier turn

#### Scenario: Journey is the primary inspector view
- **WHEN** the status inspector is rendered for a new page session
- **THEN** the journey view is selected before the existing technical Route, Plan, Context, Memory, Knowledge, Evidence, and Debug views

#### Scenario: Technical inspector remains available
- **WHEN** a developer needs detailed diagnostics
- **THEN** the existing technical inspector views remain available without duplicating their raw payloads in the journey view

### Requirement: Honest pending-request visualization

The journey view MUST distinguish a known overall request-in-progress state from unknown internal module progress.

#### Scenario: Route request is pending
- **WHEN** a user submits a question and the route or route-and-invoke request has not returned
- **THEN** the view marks the question as received, shows the overall controller as processing, and leaves downstream internal stages waiting

#### Scenario: Pending request lasts longer than expected
- **WHEN** a route request remains pending for an extended duration
- **THEN** the view continues to show the overall processing state and MUST NOT advance Context, Router, Agent, or Memory nodes using fixed timers

#### Scenario: Request fails before a route response
- **WHEN** the request fails without returning a RouteResponse
- **THEN** the overall request and response outcome are shown as failed while unobserved downstream stages are not shown as completed

### Requirement: Journey projection uses selected-turn evidence

The journey view SHALL derive completed, skipped, active, and failed stages only from the selected ConversationTurn and its existing response and trace data.

#### Scenario: Completed route-and-invoke turn
- **WHEN** the selected turn contains a RouteResponse and a successful InvocationResult
- **THEN** the view shows completed input, context preparation, routing decision, Agent handoff, Agent execution, and response stages using that turn's data

#### Scenario: Older turn is selected
- **WHEN** the user selects an earlier conversation turn
- **THEN** every journey node is recomputed from that earlier turn and no state from the latest turn is reused

#### Scenario: Global debug data differs from selected turn
- **WHEN** Memory or Knowledge debug management data contains records unrelated to the selected turn
- **THEN** those global records do not change the selected turn's journey nodes

### Requirement: Plain-language architecture stages

The journey view SHALL present the main flow with user-facing Chinese labels and concise summaries suitable for non-technical viewers.

#### Scenario: Standard Agent route
- **WHEN** a request is routed to an Agent
- **THEN** the main flow communicates receiving the question, preparing reference information, controller judgment, handing off to a business assistant, assistant processing, returning the result, and memory formation

#### Scenario: Memory or Knowledge participates
- **WHEN** the selected turn's Memory or Knowledge Context reports actual items or a meaningful non-empty status
- **THEN** the reference-information stage indicates the participation of historical memory or knowledge material without exposing provider or collection implementation details

#### Scenario: Memory or Knowledge does not participate
- **WHEN** the selected turn has no Memory or Knowledge evidence
- **THEN** the journey shows the corresponding capability as unused or skipped rather than failed

### Requirement: Route outcome branches remain truthful

The journey view SHALL adapt Agent-related stages to the actual route decision instead of requiring every request to invoke an Agent.

#### Scenario: Direct reply
- **WHEN** the route decision returns a direct reply without an Agent invocation
- **THEN** the view marks controller judgment and response as completed and marks Agent handoff and execution as skipped with a direct-reply explanation

#### Scenario: Clarification required
- **WHEN** the route decision asks the user to clarify or provide missing input
- **THEN** the view explains that more information is required and does not imply that an Agent executed

#### Scenario: Unsupported request
- **WHEN** the route decision reports no supported Agent or capability
- **THEN** the view shows the unsupported outcome in plain language and marks Agent handoff and execution as skipped

#### Scenario: Route-only mode
- **WHEN** the selected turn was submitted in route-only mode
- **THEN** the view shows the observed routing result and marks Agent execution as not performed rather than failed

### Requirement: Agent and Plan presentation

The journey view SHALL identify selected Agents and multi-step collaboration using existing registry and Plan data.

#### Scenario: Selected Agent has a friendly name
- **WHEN** the target agent_id matches an Agent in the current registry
- **THEN** the handoff stage displays the Agent's friendly name as the primary label and keeps the technical agent_id in optional details

#### Scenario: Selected Agent is not in the current registry
- **WHEN** the selected turn references an Agent that is absent from the current registry
- **THEN** the view falls back to a safely truncated agent_id without breaking the layout

#### Scenario: Route response contains a Plan
- **WHEN** the selected turn contains a Plan with one or more steps
- **THEN** the journey summarizes the number of collaborative steps and displays existing step and Agent statuses without inventing progress for unexecuted steps

### Requirement: Asynchronous memory formation state

The journey view SHALL use the selected turn's existing Memory Request Trace state to explain post-response memory formation.

#### Scenario: Memory formation is pending
- **WHEN** the selected turn's Memory Trace is loading or pending after the response
- **THEN** the memory-formation stage is shown as active without changing completed routing or invocation stages

#### Scenario: Memory formation completes
- **WHEN** the selected turn's Memory Request Trace reaches a successful terminal stage
- **THEN** the memory-formation stage is shown as completed

#### Scenario: Memory formation is not triggered
- **WHEN** the trace reports that formation was disabled, suppressed, or not triggered
- **THEN** the memory-formation stage is shown as skipped with a non-error explanation

#### Scenario: Memory formation fails
- **WHEN** the selected turn's Memory Trace reports an error or failed terminal stage
- **THEN** the memory-formation stage is shown as failed while the earlier response result remains independently represented

### Requirement: Journey node details

The journey view SHALL provide read-only, on-demand details for individual nodes while keeping the default diagram concise.

#### Scenario: Open node details
- **WHEN** a user activates a journey node that has diagnostic details
- **THEN** the UI opens an accessible detail dialog containing only fields relevant to that node

#### Scenario: Default journey rendering
- **WHEN** no node detail dialog is open
- **THEN** the diagram does not show raw JSON, long request identifiers, provider credentials, collection configuration, or full internal payloads

#### Scenario: Close node details
- **WHEN** the user presses Escape, activates the close control, or dismisses the dialog using supported backdrop behavior
- **THEN** focus returns safely to the journey view without changing the selected conversation turn

### Requirement: Stable and responsive journey layout

The journey view SHALL remain readable and interactive across supported desktop and narrow viewport layouts.

#### Scenario: Desktop right rail
- **WHEN** the journey is rendered in the existing desktop right rail
- **THEN** stages use a vertically scannable layout with stable connectors, icons, labels, and state indicators

#### Scenario: Narrow viewport
- **WHEN** the workspace stacks for a narrow viewport
- **THEN** the journey fits the available width without horizontal page scrolling or text overlapping adjacent stages

#### Scenario: Long Agent or status text
- **WHEN** a node contains a long Agent name, identifier, or summary
- **THEN** the node constrains or truncates the text while preserving the full value in its details when appropriate

#### Scenario: Keyboard navigation
- **WHEN** a keyboard user navigates the journey
- **THEN** actionable nodes have visible focus states and can be opened without a pointer device
