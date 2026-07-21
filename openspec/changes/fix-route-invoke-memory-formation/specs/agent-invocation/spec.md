## MODIFIED Requirements

### Requirement: Agent runs and results are persisted
The system SHALL persist each Agent execution attempt as an Agent Run and final or intermediate outputs as Agent Results and Agent Events. When an invocation belongs to a Canonical Turn, Run creation and Turn activity attachment SHALL be atomic, and terminal Run/Result persistence, Turn completion, and the `turn.completed` Outbox event SHALL be committed atomically and idempotently.

#### Scenario: Agent invocation succeeds
- **WHEN** an invoker completes successfully
- **THEN** the system records run status `completed`, stores the output and artifact references, completes the owned Canonical Turn, persists the unique Outbox event, and returns the result

#### Scenario: Agent invocation fails
- **WHEN** an invoker times out, raises an error, or returns invalid output
- **THEN** the system records run status `failed` or `invalid_output` with a structured error and closes the owned Canonical Turn with the corresponding terminal semantic response

#### Scenario: Canonical completion persistence fails
- **WHEN** any required Run, Result, Turn, or Outbox write fails during terminal persistence
- **THEN** the terminal transaction rolls back and the system MUST NOT report a successful route-and-invoke completion

#### Scenario: Direct invoke has no Canonical Turn
- **WHEN** an authorized explicit invocation is intentionally executed outside a Canonical Turn workflow
- **THEN** the system persists its Run/Result using the documented direct-invoke behavior and MUST NOT fabricate or ambiguously associate a Turn
