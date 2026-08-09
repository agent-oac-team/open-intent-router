## MODIFIED Requirements

### Requirement: Router builds candidate Agents before LLM routing
The system SHALL load enabled Agent definitions and filter them by access policy, including entitlement admission, before invoking trigger analysis, Evidence Provider, fixed route overrides, or the LLM routing prompt.

#### Scenario: User has entitlement access to candidate Agent
- **WHEN** an Agent is enabled, its tenant and other policy dimensions match, and its `any_entitlements` intersects the trusted PrincipalClaims.entitlements
- **THEN** the Agent can appear in the candidate list passed to routing

#### Scenario: User lacks entitlement access to Agent
- **WHEN** an Agent access policy does not match the trusted PrincipalClaims, including a missing entitlement intersection
- **THEN** the Agent MUST NOT appear in trigger, Evidence, override, plan-building, or LLM candidate inputs

#### Scenario: Trigger matches an unauthorized Agent
- **WHEN** positive keywords, examples, or other trigger evidence match an Agent excluded by access policy
- **THEN** the trigger MUST NOT restore that Agent to the candidate set or reveal it to the LLM

#### Scenario: No authorized Agent remains
- **WHEN** access policy filtering removes every enabled Agent
- **THEN** the Router returns a structured unsupported decision without calling Evidence or LLM routing for an Agent target

### Requirement: Router output is strictly validated
The system SHALL validate Evidence overrides, LLM output, generated plans, and normalized route output against the filtered candidate Agent set and action-specific schema invariants before returning a response.

#### Scenario: Target Agent is outside candidates
- **WHEN** Evidence or the LLM returns `target_agent_id` that is not in the filtered candidate Agent list
- **THEN** the system rejects that target and returns a safe fallback decision or structured routing error

#### Scenario: Plan contains unauthorized Agent step
- **WHEN** a generated or restored Plan contains any step whose Agent is not in the filtered candidate Agent list
- **THEN** the system rejects the Plan before it can be displayed, confirmed, or executed

#### Scenario: Show plan action omits plan
- **WHEN** the LLM returns `action=show_plan` without a plan
- **THEN** the system treats the output as invalid and returns a safe fallback decision or structured routing error

#### Scenario: Continue Agent targets a different Agent
- **WHEN** the LLM returns `action=continue_agent` and the target does not match the current Agent context
- **THEN** the system rejects the decision unless the relation is explicitly a valid Agent switch and the new target remains authorized
