## ADDED Requirements

### Requirement: Local visual test UI

The system SHALL provide a local visual UI for testing intent routing, Agent configuration, route decisions, and invocation results.

#### Scenario: Open local test UI

- **WHEN** a developer starts the frontend test UI and opens it in a browser
- **THEN** the UI displays service readiness, registry status, current routing provider, and available test panels

#### Scenario: Backend unavailable

- **WHEN** the UI cannot reach the backend health or readiness endpoint
- **THEN** the UI displays a clear unavailable state and does not pretend that routing tests can be executed

### Requirement: Agent and intent configuration

The UI SHALL allow developers to view, create, edit, enable, disable, and delete Agent definitions through existing backend Agent APIs.

#### Scenario: View configured Agents

- **WHEN** the UI loads the Agent configuration panel
- **THEN** it lists current Agents with agent_id, name, type, enabled status, capabilities, trigger keywords, and access policy summary

#### Scenario: Create or edit Agent intent metadata

- **WHEN** a developer creates or edits an Agent in the UI
- **THEN** the UI allows editing description, capabilities, trigger keywords, positive examples, negative examples, required inputs, access policy, invocation type, invocation config, and ui_handoff config

#### Scenario: File registry is read-only

- **WHEN** the backend registry source is file-only
- **THEN** the UI disables mutation actions or shows a clear message that Agent CRUD requires a database-backed registry

### Requirement: Conversation test panel

The UI SHALL provide a conversation panel that sends RouteRequest payloads and displays route and invocation responses.

#### Scenario: Send route-only test message

- **WHEN** a developer enters a message and submits it in route-only mode
- **THEN** the UI calls `POST /api/v1/route` and displays decision action, target_agent_id, confidence, reason, message, context, evidence, and plan

#### Scenario: Send route-and-invoke test message

- **WHEN** a developer enters a message and submits it in route-and-invoke mode
- **THEN** the UI calls `POST /api/v1/route-and-invoke` and displays both the route response and invocation result

#### Scenario: Test current Agent chat context

- **WHEN** a developer selects `source=agent_chat` and provides a current_agent
- **THEN** the UI includes current_agent in the request and displays whether the decision is continue_agent, open_agent, exit_agent, clarify, or unsupported

### Requirement: Route result inspection

The UI SHALL expose raw and summarized route debugging information without leaking secrets.

#### Scenario: Inspect route decision

- **WHEN** a route response is returned
- **THEN** the UI displays a summary card and a raw JSON viewer for the full response

#### Scenario: Inspect UI handoff

- **WHEN** the selected Agent returns a ui_handoff invocation result
- **THEN** the UI displays handoff mode, route, params, and input so the developer can verify frontend jump behavior

### Requirement: Plan and event inspection

The UI SHALL display Plan status and allow developers to inspect plan progression related to test conversations.

#### Scenario: Show plan from route response

- **WHEN** a route response includes decision.action `show_plan`
- **THEN** the UI displays plan_id, plan status, current_step_id, and all plan steps

#### Scenario: Refresh plan status

- **WHEN** a developer refreshes a plan by plan_id
- **THEN** the UI calls `GET /api/v1/plans/{plan_id}` and updates the displayed status and step states
