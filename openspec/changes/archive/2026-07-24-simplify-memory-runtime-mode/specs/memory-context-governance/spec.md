## MODIFIED Requirements

### Requirement: Memory recall uses declared scopes
The system SHALL recall Memory only from scopes allowed by tenant/user policy and the applicable route or target Agent context policy. Global Memory enablement MUST NOT grant an Agent undeclared scopes.

#### Scenario: On mode performs route-stage recall
- **WHEN** effective `MEMORY_MODE=on` and the governed route context is assembled
- **THEN** route-stage recall considers only `user_preference` and `stable_fact` after tenant, subject, user, budget, TTL, and conflict policy checks

#### Scenario: Off or observe mode assembles route context
- **WHEN** effective `MEMORY_MODE` is `off` or `observe`
- **THEN** no recalled Memory item is injected into the route request or router prompt

#### Scenario: Agent declares supported memory scopes
- **WHEN** an Agent Definition uses `context.memory.mode=prefetch` and explicitly declares scopes such as `user_preference`, `stable_fact`, `task_memory`, `artifact_reference`, or `session_summary`
- **THEN** Agent-stage recall considers only those scopes after tenant, subject, user, TTL, budget, and current-task policy checks

#### Scenario: Agent omits memory scopes
- **WHEN** an Agent has Memory disabled or does not declare any Memory scope
- **THEN** Agent-stage recall returns an empty or disabled Memory context and does not inherit route defaults or global mode scopes

#### Scenario: Global mode is on but Agent is disabled
- **WHEN** effective `MEMORY_MODE=on` and the selected Agent has `context.memory.mode=disabled`
- **THEN** the Agent invocation receives no Agent-stage recalled Memory from the global mode

#### Scenario: User lacks access to a memory subject
- **WHEN** recall would return Memory for a subject the current user cannot access
- **THEN** the system excludes that Memory and records a redacted exclusion in debug or audit metadata
