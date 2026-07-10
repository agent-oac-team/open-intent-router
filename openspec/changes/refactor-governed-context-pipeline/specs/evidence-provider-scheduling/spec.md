## ADDED Requirements

### Requirement: Evidence Providers cannot write directly to Router prompt
Ordinary Evidence Provider outputs SHALL enter the Context Pipeline as Context Candidates and MUST NOT be appended directly to Router Prompt or raw Route metadata as model-bound context.

#### Scenario: Provider returns evidence snippets
- **WHEN** an Evidence Provider returns ordinary evidence snippets
- **THEN** the snippets pass through permission, deduplication, budget, Projection, and Trace processing before model use

#### Scenario: Evidence is dropped
- **WHEN** an Evidence Candidate is denied, duplicated, expired, invalid, or over budget
- **THEN** its raw content and unbounded structured value do not appear in Router Prompt

## MODIFIED Requirements

### Requirement: Route-stage evidence is separate from Agent execution knowledge
The system SHALL keep route-stage Evidence purpose distinct from Agent execution Knowledge purpose while processing both through the shared Context Pipeline and common governance controls.

#### Scenario: Router needs evidence for intent
- **WHEN** the route flow needs hints or deterministic fixed-question behavior
- **THEN** it uses Evidence Provider scheduling for `route_decision` before LLM routing

#### Scenario: Agent needs knowledge for execution
- **WHEN** a target Agent needs document or source evidence for task execution
- **THEN** it receives governed `knowledge_context` or uses controlled retrieval according to Agent context configuration

#### Scenario: Evidence and knowledge both exist
- **WHEN** route-stage Evidence and Agent execution Knowledge are both available
- **THEN** the system records them with separate purpose, consumer, source, policy, and Trace metadata
- **AND** only items visible to each consumer enter its Projection

#### Scenario: Router knowledge supports direct reply
- **WHEN** Router-stage Knowledge retrieval returns reliable evidence for a direct answer
- **THEN** that knowledge remains distinguishable from fixed-question route overrides and Agent execution knowledge

### Requirement: Evidence scheduling observes budget and failure policy
The system SHALL apply timeout, error, permission, deduplication, and final Router Projection budget controls to ordinary Evidence Provider outputs.

#### Scenario: Evidence provider times out
- **WHEN** a non-required Evidence Provider exceeds its timeout
- **THEN** the Pipeline records the Provider timeout and continues routing with remaining eligible context

#### Scenario: Evidence snippets exceed budget
- **WHEN** Provider Evidence exceeds the route-stage Evidence or total Projection budget
- **THEN** lower-value Evidence is dropped, truncated, or reference-projected before LLM routing
- **AND** Context Trace records the outcome

#### Scenario: Strong fixed question is matched
- **WHEN** a strong fixed-question route override is valid inside the access-filtered candidate set
- **THEN** that deterministic override is not discarded by ordinary Evidence snippet budget trimming
- **AND** it remains subject to Agent access policy

#### Scenario: Evidence duplicates knowledge
- **WHEN** Evidence and route-stage Knowledge represent the same source item or fact
- **THEN** the Pipeline deduplicates them before spending Router context budget while retaining purpose/source traceability
