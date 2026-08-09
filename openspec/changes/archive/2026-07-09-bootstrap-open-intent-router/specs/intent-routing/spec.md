## ADDED Requirements

### Requirement: Route API accepts generic host requests
The system SHALL expose `POST /api/v1/route` for host applications to submit user input, user context, current Agent context, frontend context, and optional plan/event references.

#### Scenario: Route request is accepted
- **WHEN** a valid route request contains `session_id`, `user`, `source`, and text input
- **THEN** the system returns a structured route response with `request_id`, `session_id`, `decision`, `context`, `plan`, and `invocation`

#### Scenario: Invalid route request is rejected
- **WHEN** a route request is missing required input fields or uses an unsupported input type
- **THEN** the system returns a validation error without calling the LLM router

### Requirement: Router builds candidate Agents before LLM routing
The system SHALL load enabled Agent definitions and filter them by access policy and request context before constructing the LLM routing prompt.

#### Scenario: User has access to candidate Agent
- **WHEN** an Agent is enabled and its access policy matches the request user roles, groups, tenants, or attributes
- **THEN** the Agent can appear in the candidate list passed to routing

#### Scenario: User lacks access to Agent
- **WHEN** an Agent access policy does not match the request user context
- **THEN** the Agent MUST NOT appear in the candidate list passed to routing

### Requirement: Router supports OpenAI-compatible and Mock LLM clients
The system SHALL route through an `LLMClient` abstraction with OpenAI-compatible and Mock implementations.

#### Scenario: OpenAI-compatible routing is configured
- **WHEN** LLM provider settings contain base URL, model, and API key
- **THEN** the router calls the OpenAI-compatible client and parses the structured model output

#### Scenario: Mock routing is configured
- **WHEN** tests configure the Mock LLM client
- **THEN** the router returns deterministic structured decisions without external network calls

### Requirement: Router output is strictly validated
The system SHALL validate LLM output against the router output schema and enforce action-specific invariants before returning a response.

#### Scenario: Target Agent is outside candidates
- **WHEN** the LLM returns `target_agent_id` that is not in the filtered candidate Agent list
- **THEN** the system rejects that target and returns a safe fallback decision or structured routing error

#### Scenario: Show plan action omits plan
- **WHEN** the LLM returns `action=show_plan` without a plan
- **THEN** the system treats the output as invalid and returns a safe fallback decision or structured routing error

#### Scenario: Continue Agent targets a different Agent
- **WHEN** the LLM returns `action=continue_agent` and the target does not match the current Agent context
- **THEN** the system rejects the decision unless the relation is explicitly a valid Agent switch

### Requirement: Router supports route-only and route-and-invoke modes
The system SHALL support returning only a route decision or preparing invocation data for backend execution based on route mode and endpoint.

#### Scenario: Route-only endpoint is used
- **WHEN** the host calls `POST /api/v1/route`
- **THEN** the system returns route decision data and does not execute the target Agent

#### Scenario: Route-and-invoke endpoint is used
- **WHEN** the host calls `POST /api/v1/route-and-invoke` and routing selects an invokable Agent with complete inputs
- **THEN** the system creates an Agent run and invokes the target Agent through the matching invoker

### Requirement: Router clarifies missing required inputs
The system SHALL inspect selected Agent input requirements before invocation and return `clarify` when required slots are missing.

#### Scenario: Required input is missing
- **WHEN** a selected Agent requires a slot that cannot be extracted from user input, session context, frontend context, recent results, or artifacts
- **THEN** the system returns `action=clarify` with a message requesting the minimum required missing information

#### Scenario: Required input is present
- **WHEN** all required slots are available and input schema validation passes
- **THEN** the system builds `invocation.input` and proceeds with route response or Agent invocation
