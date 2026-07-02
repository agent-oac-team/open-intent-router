## ADDED Requirements

### Requirement: Router supports the complete route action set
The router SHALL support and validate the complete route action set defined by the core schema.

#### Scenario: Reply action
- **WHEN** a request can be answered without opening or continuing an Agent
- **THEN** the route decision may use `action=reply` without a target Agent

#### Scenario: Clarify action
- **WHEN** required information is missing or confidence is too low to proceed safely
- **THEN** the route decision uses `action=clarify` and provides a user-visible clarification through `assistant_message`

#### Scenario: Open agent action
- **WHEN** the request should enter a target Agent context
- **THEN** the route decision uses `action=open_agent` and the target Agent MUST be included in `context.candidate_agent_ids`

#### Scenario: Continue agent action
- **WHEN** the request continues the current Agent context
- **THEN** the route decision uses `action=continue_agent` and `target_agent_id` MUST equal `context.current_agent_id`

#### Scenario: Exit agent action
- **WHEN** the user asks to leave the current Agent context
- **THEN** the route decision uses `action=exit_agent` and does not invoke a new Agent

#### Scenario: Unsupported action
- **WHEN** no available candidate can handle the request
- **THEN** the route decision uses `action=unsupported` and provides a user-visible unsupported message

#### Scenario: Silent action
- **WHEN** the router determines that no user-visible reply is required
- **THEN** the route decision may use `action=silent` without requiring an Agent invocation

### Requirement: Candidate filtering happens before LLM routing
The router SHALL filter candidate Agents before calling the LLM or Evidence Provider.

#### Scenario: Access filtering
- **WHEN** the router receives a request from a user
- **THEN** it first removes disabled Agents and Agents unavailable to that user by role, group, tenant, or required attributes

#### Scenario: Tag filtering
- **WHEN** one or more available Agents match the request by configured tags, capabilities, trigger terms, or metadata routing tags
- **THEN** only the matched Agents are passed to the Evidence Provider and LLM as route candidates

#### Scenario: No tag match fallback
- **WHEN** access filtering leaves available Agents but no Agent matches the tag filter
- **THEN** all currently available Agents are passed to the Evidence Provider and LLM and the route context or Route Log records `tag_filter=no_match_fallback_all_available`

#### Scenario: No available agents
- **WHEN** access filtering leaves no available Agents
- **THEN** the router returns an unsupported response and MUST NOT call the LLM with an empty or unauthorized candidate set

#### Scenario: Evidence provider candidate boundary
- **WHEN** the Evidence Provider receives candidate Agent IDs
- **THEN** those IDs MUST be within the filtered candidate set and MUST NOT include Agents unavailable to the user

#### Scenario: LLM candidate boundary
- **WHEN** the LLM route call is made
- **THEN** the candidates array contains only Agents in the filtered candidate set

### Requirement: Tag filter uses a generic metadata convention
The tag filter SHALL use generic Agent configuration fields and SHALL NOT introduce business-specific core concepts.

#### Scenario: Agent tags match
- **WHEN** an Agent has a matching value in `tags`
- **THEN** the Agent can enter the filtered candidate set

#### Scenario: Capability tags match
- **WHEN** an Agent has a matching value in `capabilities`
- **THEN** the Agent can enter the filtered candidate set

#### Scenario: Trigger text matches
- **WHEN** an Agent has a matching trigger keyword or positive example
- **THEN** the Agent can enter the filtered candidate set

#### Scenario: Metadata routing tags match
- **WHEN** an Agent has matching values in generic metadata keys such as `intent_tags` or `routing_tags`
- **THEN** the Agent can enter the filtered candidate set without adding new first-class business fields

### Requirement: Missing required inputs trigger clarification
The router SHALL ask for missing required inputs before creating an invocation preview or executing a target Agent.

#### Scenario: Required input missing
- **WHEN** the selected target Agent has required inputs that cannot be built from the current request
- **THEN** the route decision becomes `action=clarify`, the response has no invocation preview, and `assistant_message` asks for the missing information

#### Scenario: Collect input next action
- **WHEN** the host application needs structured guidance to collect missing inputs
- **THEN** the response may include `next_action.type=collect_input` with missing field names in metadata

#### Scenario: Required input present
- **WHEN** the selected target Agent's required inputs are present
- **THEN** the route response may include an invocation preview or proceed according to the selected route endpoint and execution policy

### Requirement: Low confidence can trigger clarification
The router SHALL support a conservative configurable confidence threshold for clarification.

#### Scenario: Confidence below threshold
- **WHEN** the normalized route decision has confidence below the configured low-confidence threshold and no stronger override applies
- **THEN** the router returns `action=clarify`, provides a user-visible clarification through `assistant_message`, and records the confidence and threshold in Route or Debug metadata

#### Scenario: Confidence above threshold
- **WHEN** the normalized route decision has confidence at or above the configured low-confidence threshold
- **THEN** the router may continue with the selected action subject to normal validation

#### Scenario: Threshold is conservative by default
- **WHEN** no custom threshold is configured
- **THEN** the default threshold is conservative enough to avoid frequent interruption of otherwise valid routing decisions

### Requirement: Route paths keep intent behavior consistent
The system SHALL apply the same action validation, candidate filtering, and missing-input clarification rules across route-only, route-and-invoke, and route-and-execute.

#### Scenario: Route-only missing input
- **WHEN** route-only selects an Agent but required input is missing
- **THEN** it returns a clarification response instead of a deferred invocation preview

#### Scenario: Route-and-invoke missing input
- **WHEN** route-and-invoke selects an Agent but required input is missing
- **THEN** it returns the clarification route and does not invoke the Agent

#### Scenario: Route-and-execute missing input
- **WHEN** route-and-execute selects an Agent or plan step but required input is missing
- **THEN** it returns a clarification or collect-input next action instead of executing the Agent
