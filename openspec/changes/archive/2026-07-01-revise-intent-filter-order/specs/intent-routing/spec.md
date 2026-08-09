## MODIFIED Requirements

### Requirement: Candidate filtering happens before LLM routing
The router SHALL establish the access-filtered candidate Agent boundary before calling the LLM or Evidence Provider, and SHALL treat tag or semantic filtering as observe-only recall metadata in this version.

#### Scenario: Access filtering
- **WHEN** the router receives a request from a user
- **THEN** it first removes disabled Agents and Agents unavailable to that user by role, group, tenant, or required attributes

#### Scenario: Tag filtering is observe-only
- **WHEN** one or more available Agents match the request by configured tags, capabilities, trigger terms, or metadata routing tags
- **THEN** the router records the matched Agent IDs and match details, but all access-filtered Agents remain candidates for the Evidence Provider and LLM

#### Scenario: No tag match keeps all available candidates
- **WHEN** access filtering leaves available Agents but no Agent matches the tag filter
- **THEN** all currently available Agents are passed to the Evidence Provider and LLM and the route context or Route Log records that no tag match was applied

#### Scenario: No available agents
- **WHEN** access filtering leaves no available Agents
- **THEN** the router returns an unsupported response and MUST NOT call the LLM with an empty or unauthorized candidate set

#### Scenario: Evidence provider candidate boundary
- **WHEN** the Evidence Provider receives candidate Agent IDs
- **THEN** those IDs MUST equal the access-filtered candidate set and MUST NOT include Agents unavailable to the user

#### Scenario: LLM candidate boundary
- **WHEN** the LLM route call is made
- **THEN** the candidates array contains all access-filtered Agents and excludes unavailable Agents

## ADDED Requirements

### Requirement: Strong deterministic routes bypass LLM routing
The router SHALL skip LLM routing when an earlier deterministic stage produces a valid strong route inside the access-filtered candidate set.

#### Scenario: Strong route override
- **WHEN** an Evidence Provider returns a strong route override to an available Agent
- **THEN** the router returns the route override and MUST NOT call the LLM for that request

#### Scenario: Strong route override outside candidates
- **WHEN** an Evidence Provider returns a strong route override to an unavailable Agent
- **THEN** the router MUST return a user-visible no-permission response, MUST NOT route to the unavailable Agent, and MUST NOT call the LLM for fallback intent judgment
