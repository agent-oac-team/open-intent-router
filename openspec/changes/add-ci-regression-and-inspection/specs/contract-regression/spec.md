## ADDED Requirements

### Requirement: OpenSpec changes are validated
The project SHALL validate OpenSpec change artifacts with strict validation before a change is treated as implementation-ready or ready for archive.

#### Scenario: Changed OpenSpec artifact is validated
- **WHEN** a pull request modifies files under `openspec/changes/<change-name>/`
- **THEN** the change can be validated with `openspec validate <change-name> --strict`

#### Scenario: OpenSpec validation fails
- **WHEN** strict validation reports malformed specs, missing scenarios, invalid tasks, or inconsistent artifacts
- **THEN** the change is not considered ready for implementation or archive

### Requirement: Core API response contracts are regression tested
The project SHALL maintain regression coverage for stable API response shapes that host applications depend on.

#### Scenario: Route response contract changes
- **WHEN** a change modifies `RouteResponse`, route decisions, plan payloads, next actions, or assistant messages
- **THEN** tests assert the expected response structure and compatibility behavior

#### Scenario: Invocation response contract changes
- **WHEN** a change modifies `route-and-invoke`, `route-and-execute`, plan execution, run results, or error payloads
- **THEN** tests assert the expected response structure and error semantics

### Requirement: Router and registry behavior are regression tested
The project SHALL maintain automated regression coverage for routing decisions, candidate filtering, access policies, fixed-question overrides, and unavailable-agent behavior.

#### Scenario: Candidate filtering changes
- **WHEN** a change modifies registry loading, availability checks, access policy, tag matching, or candidate pruning
- **THEN** tests verify that routing never expands candidates beyond the caller's allowed agents

#### Scenario: Fixed-question override changes
- **WHEN** a change modifies Evidence Provider fixed-question behavior or strong routing overrides
- **THEN** tests verify that overrides still respect agent availability and access policy

### Requirement: Plan execution behavior is regression tested
The project SHALL maintain automated regression coverage for plan contracts, execution policies, dependency order, pause conditions, resume behavior, and terminal states.

#### Scenario: Plan contract changes
- **WHEN** a change modifies multi-intent routing, `plan`, `decision.action`, `execution_policy`, or `next_action`
- **THEN** tests verify that `plan` presence remains the primary multi-intent contract and `show_plan` remains compatibility behavior

#### Scenario: Plan executor changes
- **WHEN** a change modifies `PlanExecutor`, `InvocationService`, plan actions, or step status transitions
- **THEN** tests verify dependency ordering, confirmation, UI handoff pause, missing-input pause, resume, completion, and failure behavior

### Requirement: Security and log-safety behavior are regression tested
The project SHALL maintain automated regression coverage for admin authorization, safe runtime configuration, context redaction, and route/run log safety.

#### Scenario: Admin security changes
- **WHEN** a change modifies Admin APIs, token handling, local mode, runtime config, or request origin behavior
- **THEN** tests verify local loopback behavior, non-local token requirements, and configured-token enforcement

#### Scenario: Log redaction changes
- **WHEN** a change modifies route logs, run logs, Context Pack metadata, LLM Provider configuration, or invocation configuration
- **THEN** tests verify that API keys, authorization headers, secret-like metadata, and unbounded raw context are not persisted or exposed in debug output

### Requirement: External providers are tested without real credentials
The project SHALL test LLM Provider and invoker compatibility using mocks, fixtures, or local test servers rather than real external credentials in CI.

#### Scenario: LLM compatibility changes
- **WHEN** a change modifies OpenAI-compatible LLM parsing, prompt payloads, fallback plans, or malformed output handling
- **THEN** CI tests the behavior with mocked responses and no real provider API key

#### Scenario: HTTP invoker compatibility changes
- **WHEN** a change modifies HTTP invocation behavior
- **THEN** tests use safe local or mocked HTTP behavior and do not require real third-party service credentials
