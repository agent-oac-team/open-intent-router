# evidence-provider-scheduling Specification

## Purpose
TBD - created by archiving change add-agent-context-memory-knowledge. Update Purpose after archive.
## Requirements
### Requirement: Evidence Providers can be scheduled
The system SHALL support scheduling one or more Evidence Providers before LLM routing according to configured provider policy.

#### Scenario: Multiple providers are configured
- **WHEN** more than one Evidence Provider is enabled
- **THEN** the router or scheduler selects and calls providers according to configured priority, applicability, and timeout policy

#### Scenario: Provider is not applicable
- **WHEN** a provider does not apply to the current route request, candidate set, user, tenant, or purpose
- **THEN** the scheduler skips that provider and records the skip reason

#### Scenario: Provider returns weak evidence
- **WHEN** a provider returns weak hints, candidate IDs, or evidence snippets
- **THEN** the router adds them to context or debug metadata without shrinking the access-filtered LLM candidate set

### Requirement: Fixed questions remain deterministic route rules
The system SHALL keep fixed-question matching as an Evidence Provider path with special deterministic precedence for strong question-to-intent mappings.

#### Scenario: Strong fixed question maps to available Agent
- **WHEN** a strong fixed-question match returns a route override whose target Agent is inside the access-filtered candidate set
- **THEN** the router returns the fixed route without calling the LLM

#### Scenario: Strong fixed question maps to unavailable Agent
- **WHEN** a strong fixed-question match targets an Agent outside the access-filtered candidate set
- **THEN** the router returns a user-visible no-permission response without calling the LLM for fallback intent judgment

#### Scenario: Weak fixed question match occurs
- **WHEN** a fixed question is configured as weak
- **THEN** its intent hint and candidate IDs are included as weak context only and MUST NOT shrink the LLM candidate set

### Requirement: Fixed questions only map question to intent
The system SHALL treat fixed questions in this change as question-to-intent or question-to-route mappings, not fixed answer content.

#### Scenario: Fixed question has route override
- **WHEN** a fixed question matches and contains a route override
- **THEN** the router applies route override behavior according to fixed-question strength and access policy

#### Scenario: Fixed answer is requested
- **WHEN** a future configuration attempts to use fixed questions as direct FAQ answer content
- **THEN** this change does not define that behavior and the content MUST NOT bypass KnowledgeSource or Agent response policy

### Requirement: Route-stage evidence is separate from Agent execution knowledge
The system SHALL keep route-stage evidence scheduling distinct from Agent execution knowledge retrieval.

#### Scenario: Router needs evidence for intent
- **WHEN** the route flow needs hints or deterministic fixed-question behavior
- **THEN** it uses Evidence Provider scheduling before LLM routing

#### Scenario: Agent needs knowledge for execution
- **WHEN** a target Agent needs document or source evidence for task execution
- **THEN** it receives governed `knowledge_context` or uses controlled retrieval according to Agent context configuration

#### Scenario: Evidence and knowledge both exist
- **WHEN** route-stage evidence and Agent execution knowledge are both available
- **THEN** the system records them with separate purpose and source metadata for audit and debugging

### Requirement: Evidence scheduling observes budget and failure policy
The system SHALL apply timeout, error, and budget controls to Evidence Provider outputs.

#### Scenario: Evidence provider times out
- **WHEN** a non-required Evidence Provider exceeds its timeout
- **THEN** the router records the timeout and continues routing with remaining context

#### Scenario: Evidence snippets exceed budget
- **WHEN** provider evidence exceeds route-stage Context Pack budget
- **THEN** lower-value evidence is dropped or summarized before LLM routing

#### Scenario: Strong fixed question is matched
- **WHEN** a strong fixed-question route override is valid
- **THEN** that deterministic override is not discarded by ordinary evidence snippet budget trimming

