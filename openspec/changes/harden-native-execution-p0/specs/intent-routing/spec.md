## MODIFIED Requirements

### Requirement: Route API accepts generic host requests
The system SHALL expose `POST /api/v1/route` for host applications to submit user input, compatibility user context, current Agent context, frontend context, and optional plan/event references, and SHALL derive the authorization-bearing User Context from the current request's trusted Native Principal.

#### Scenario: Route request is accepted
- **WHEN** a valid route request contains `session_id`, `user`, `source`, and text input together with a valid Principal
- **THEN** the system binds the Principal-derived User Context and returns a structured route response with `request_id`, `session_id`, `decision`, `context`, `plan`, and `invocation`

#### Scenario: Body user claims additional permissions
- **WHEN** a route request body supplies roles, groups, entitlements, tenant, or authorization attributes that are not present in the trusted Principal
- **THEN** those claims do not enter candidate filtering, Context retrieval, routing, or invocation

#### Scenario: Invalid route request is rejected
- **WHEN** a route request is missing required input fields, uses an unsupported input type, or lacks required trusted identity
- **THEN** the system returns a validation or authentication error without calling the LLM router

### Requirement: Router builds candidate Agents before LLM routing
The system SHALL load enabled Agent definitions and filter them once by access policy and the trusted request User Context before constructing the LLM routing prompt. The resulting Candidate Set SHALL be scoped to that request and MUST NOT be reused by a later trusted request.

#### Scenario: User has access to candidate Agent
- **WHEN** an Agent is enabled and its access policy matches the trusted Principal roles, groups, tenant, entitlements, and attributes
- **THEN** the Agent can appear in the Candidate Set passed to routing

#### Scenario: User lacks access to Agent
- **WHEN** an Agent access policy does not match the trusted request User Context
- **THEN** the Agent MUST NOT appear in the Candidate Set passed to routing

#### Scenario: Route and invoke occur in one request
- **WHEN** routing selects an Agent and the same request proceeds to invocation
- **THEN** invocation consumes the Candidate Set produced by routing without recomputing Agent access policy

#### Scenario: A delayed Plan is executed later
- **WHEN** a Plan created by an earlier route is confirmed, executed, or resumed in a new request
- **THEN** that new request forms its own Candidate Set before invoking any Plan Step
