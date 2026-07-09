## ADDED Requirements

### Requirement: Runtime test mode visibility

The system SHALL expose the current routing test configuration to the UI using a safe, read-only, secret-redacted endpoint or equivalent bootstrap payload.

#### Scenario: Load runtime routing status

- **WHEN** the UI starts
- **THEN** it can display current router provider, model, prompt file, route mode, registry backend, storage backend, and evidence provider enabled state

#### Scenario: Secrets are redacted

- **WHEN** the UI requests runtime routing status
- **THEN** the response does not include API keys, admin tokens, database passwords, or secret HTTP headers

### Requirement: Local reply and LLM mode guidance

The UI SHALL let developers choose between local Mock-style testing and LLM-backed testing in a way that reflects the backend's actual configured provider.

#### Scenario: Backend is configured for mock routing

- **WHEN** `ROUTER_LLM_PROVIDER` is `mock`
- **THEN** the UI shows local Mock routing as active and explains that no external model call will be made

#### Scenario: Backend is configured for OpenAI-compatible routing

- **WHEN** `ROUTER_LLM_PROVIDER` is `openai_compatible`
- **THEN** the UI shows LLM routing as active and displays the configured model and base URL without showing the API key

#### Scenario: User selects a mode that differs from backend config

- **WHEN** a developer selects a UI mode that does not match the backend's actual provider
- **THEN** the UI shows a warning that changing provider requires updating `.env` and restarting the backend unless a future local-only runtime override endpoint is implemented

### Requirement: Route execution mode selection

The UI SHALL allow each test message to be sent as route-only or route-and-invoke.

#### Scenario: Route-only selected

- **WHEN** route-only is selected
- **THEN** the UI sends the message to `/api/v1/route` and does not call Agent invocation endpoints

#### Scenario: Route-and-invoke selected

- **WHEN** route-and-invoke is selected
- **THEN** the UI sends the message to `/api/v1/route-and-invoke` and displays invocation result when present

### Requirement: Test request context controls

The UI SHALL allow developers to configure key RouteRequest context fields for testing.

#### Scenario: Configure user context

- **WHEN** a developer edits user id, roles, groups, tenant_id, or attributes
- **THEN** the next route request includes those values in `user`

#### Scenario: Configure session and source

- **WHEN** a developer edits session_id, source, current_agent, frontend_context, plan_id, or step_id
- **THEN** the next route request includes those values when valid for the selected source

#### Scenario: Invalid agent_chat context

- **WHEN** source is `agent_chat` and current_agent is missing
- **THEN** the UI prevents submission or displays validation feedback before sending the request
