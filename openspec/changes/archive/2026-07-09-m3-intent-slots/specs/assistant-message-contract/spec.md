## ADDED Requirements

### Requirement: Route response exposes assistant message
The route response SHALL expose an optional top-level `assistant_message` string as the primary user-visible response for chat surfaces.

#### Scenario: Assistant message is present
- **WHEN** a route request completes with a user-visible answer
- **THEN** the route response includes `assistant_message` containing the text that a normal chat window should display

#### Scenario: Assistant message is pure text
- **WHEN** the backend creates `assistant_message`
- **THEN** the value is a string and does not require the client to parse route, plan, invocation, or debug fields to render a basic chat bubble

### Requirement: Router normalization owns assistant message finalization
The Router normalization layer SHALL be responsible for the final `assistant_message` value returned to clients.

#### Scenario: LLM provides assistant message
- **WHEN** the LLM output includes `assistant_message`
- **THEN** the backend validates and normalizes it before returning the route response

#### Scenario: LLM omits assistant message
- **WHEN** the LLM output omits `assistant_message` or provides an empty value
- **THEN** the backend fills `assistant_message` from route state using deterministic fallback rules

#### Scenario: Debug fields are available
- **WHEN** route, plan, invocation, or debug fields contain internal messages
- **THEN** the backend MUST NOT construct `assistant_message` by concatenating those internal fields into a chat bubble

### Requirement: Chat surfaces prefer assistant message
Clients and the local visual test UI SHALL use `assistant_message` as the normal chat transcript source when it is present.

#### Scenario: Assistant message and decision message both exist
- **WHEN** a route response contains both `assistant_message` and `decision.message`
- **THEN** the chat transcript displays `assistant_message`

#### Scenario: Assistant message is missing
- **WHEN** a route response does not contain `assistant_message` but contains `decision.message`
- **THEN** old clients and compatibility fallback paths MAY display `decision.message`

### Requirement: Existing message fields keep scoped responsibilities
The system SHALL keep existing message and reason fields scoped to their owning state areas rather than treating them as primary chat output.

#### Scenario: Next action message exists
- **WHEN** a route response contains `next_action.message`
- **THEN** the Plan or Host collaboration area displays it and the chat transcript does not append it as an additional assistant bubble

#### Scenario: Invocation result message exists
- **WHEN** route-and-invoke returns `AgentInvocationResult.message`
- **THEN** the Result or Debug area displays it and the chat transcript does not append it as an additional assistant bubble

#### Scenario: Decision reason exists
- **WHEN** a route response contains `decision.reason`
- **THEN** the Route or Debug area displays it and the chat transcript does not treat it as the normal user-visible answer while `assistant_message` or `decision.message` is available

### Requirement: Assistant message is generated consistently across route paths
The backend SHALL provide consistent assistant-message behavior for route-only, route-and-invoke, and route-and-execute responses.

#### Scenario: Route-only request
- **WHEN** `POST /api/v1/route` returns a route response
- **THEN** the route response follows the same `assistant_message` contract as other route entry points

#### Scenario: Route-and-invoke request
- **WHEN** `POST /api/v1/route-and-invoke` returns a route response and optional invocation result
- **THEN** the nested route response uses `assistant_message` for chat output and leaves invocation result details in the result payload

#### Scenario: Route-and-execute request
- **WHEN** `POST /api/v1/route-and-execute` returns a route response, results, and next action
- **THEN** the nested route response uses `assistant_message` for chat output and leaves execution details in result or plan state
