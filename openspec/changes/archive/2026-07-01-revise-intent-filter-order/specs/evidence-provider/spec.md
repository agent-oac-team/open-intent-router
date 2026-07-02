## MODIFIED Requirements

### Requirement: Fixed-question mapping runs before LLM routing
The system SHALL allow Evidence Providers to match normalized user questions to fixed-question definitions and return intent hints, candidate Agent IDs, or route overrides. Strong fixed-question route overrides SHALL run after access filtering and before LLM routing.

#### Scenario: Strong fixed-question match occurs
- **WHEN** the user input matches a fixed question configured as a strong route override and the override target is in the access-filtered candidate set
- **THEN** the router returns or applies the mapped intent or Agent route without calling the LLM

#### Scenario: Strong fixed-question target is unavailable
- **WHEN** the user input matches a fixed question configured as a strong route override but the override target is not in the access-filtered candidate set
- **THEN** the Evidence Provider or Router MUST preserve a denied strong-match signal and the router MUST return a user-visible no-permission response without calling the LLM

#### Scenario: Weak fixed-question match occurs
- **WHEN** the user input matches a fixed question configured as a weak hint
- **THEN** the router injects the hint, matched candidate Agent IDs, and evidence into LLM route context without shrinking the LLM candidate set

#### Scenario: No fixed-question match occurs
- **WHEN** the user input does not match any fixed question
- **THEN** the router continues with normal LLM routing
