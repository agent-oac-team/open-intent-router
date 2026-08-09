## ADDED Requirements

### Requirement: Intent filter order is deterministic
The router SHALL apply pre-LLM routing filters in this order: access filtering, strong deterministic rules, semantic or tag recall signals, and finally LLM routing.

#### Scenario: Strong rule runs before recall signals
- **WHEN** a request matches a strong deterministic rule and the target Agent is available to the user
- **THEN** the router MUST return the strong-rule route without allowing tag or semantic recall signals to override it

#### Scenario: LLM runs after deterministic rules
- **WHEN** no strong deterministic rule applies within the user's available Agent boundary
- **THEN** the router may call the LLM with the access-filtered candidate Agent set and any weak recall signals in context

### Requirement: Access filtering is a hard boundary
The router SHALL treat enabled state and Agent access policy as the hard boundary for all downstream routing stages.

#### Scenario: Strong rule targets unauthorized Agent
- **WHEN** a strong deterministic rule maps to an Agent that is disabled or unavailable to the request user
- **THEN** the router MUST return a user-visible no-permission response, MUST NOT route to that Agent, and MUST NOT call the LLM for fallback intent judgment

#### Scenario: LLM selects unauthorized Agent
- **WHEN** the LLM returns a target Agent outside the access-filtered candidate set
- **THEN** the router MUST reject the decision as invalid instead of invoking or opening that Agent

### Requirement: Strong fixed-question match short-circuits LLM routing
The router SHALL treat a strong fixed-question match as a complete route decision when its target Agent is within the access-filtered candidate set.

#### Scenario: Strong fixed question is available
- **WHEN** the user input matches a fixed question configured with `strength=strong` and a route override target available to the user
- **THEN** the router returns the route override response and MUST NOT call the LLM for this request

#### Scenario: Strong fixed question includes invocation preview
- **WHEN** a strong fixed-question route selects an Agent whose required inputs are available
- **THEN** the router may attach the normal deferred invocation preview without calling the LLM

### Requirement: Tag and semantic filtering is observe-only in this version
The router SHALL preserve tag or semantic match computation for observability, but SHALL NOT use it to remove Agents from the candidate set in this version.

#### Scenario: Tag match exists
- **WHEN** one or more available Agents match the request by tags, capabilities, trigger terms, examples, or metadata routing tags
- **THEN** the router records the matched Agent IDs and match details in route metadata while still passing all access-filtered Agents to Evidence Provider and LLM

#### Scenario: No tag match exists
- **WHEN** no available Agent matches the tag or semantic filter
- **THEN** the router records a no-match status and still passes all access-filtered Agents to Evidence Provider and LLM

#### Scenario: Tag pruning is not configurable in this version
- **WHEN** the router is configured for this version
- **THEN** it MUST NOT expose a runtime configuration switch that re-enables tag or semantic candidate pruning
