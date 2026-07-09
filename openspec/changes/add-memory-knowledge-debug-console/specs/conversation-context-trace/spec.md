## ADDED Requirements

### Requirement: Conversation turns retain memory and knowledge trace
The frontend SHALL represent each submitted user interaction as a conversation turn that retains its own user message, assistant message, route response, invocation result, memory context, and knowledge context when those values are returned by the backend.

#### Scenario: Route and invoke turn stores contexts
- **WHEN** a user submits a route-and-invoke message and the backend returns invocation input containing `memory_context` and `knowledge_context`
- **THEN** the completed conversation turn stores those contexts with the same turn rather than only in global latest-response state

#### Scenario: Route-only turn handles missing invocation context
- **WHEN** a user submits a route-only message and the backend response has no invocation input
- **THEN** the completed conversation turn remains selectable and shows memory and knowledge context as unavailable, disabled, or empty without inferring use from debug repository state

#### Scenario: Failed turn preserves inspectable status
- **WHEN** a request fails before a route response is returned
- **THEN** the conversation turn records the failed assistant message and does not reuse memory or knowledge trace from a previous turn

### Requirement: Conversation UI summarizes per-turn memory and knowledge use
The frontend SHALL display a compact memory/knowledge usage summary for each completed assistant turn when trace data is available.

#### Scenario: Turn summary shows item counts
- **WHEN** a completed turn has memory and knowledge contexts with items
- **THEN** the assistant turn shows counts for used memory items, used knowledge items, and knowledge citations

#### Scenario: Turn summary shows denied sources
- **WHEN** a completed turn has `knowledge_context.metadata.denied_source_ids`
- **THEN** the assistant turn summary includes the denied source count or an equivalent visible denied indicator

#### Scenario: Empty contexts are visible
- **WHEN** a completed turn has memory or knowledge context status `empty`, `disabled`, `timeout`, or `error`
- **THEN** the assistant turn summary shows that status instead of pretending items were used

### Requirement: Status inspector follows selected conversation turn
The frontend SHALL let the user inspect a selected conversation turn, and the right-side status inspector SHALL render route, plan, context, memory, knowledge, evidence, and debug data for that selected turn.

#### Scenario: Selecting older turn updates inspector
- **WHEN** the user selects an older completed conversation turn
- **THEN** the right-side status inspector displays that turn's route response, invocation result, memory context, and knowledge context instead of the newest turn

#### Scenario: New response selects latest turn
- **WHEN** a new message completes successfully
- **THEN** the frontend selects that newly completed turn by default

#### Scenario: New conversation clears selected trace
- **WHEN** the user starts a new conversation
- **THEN** all conversation turns and selected-turn trace are cleared

### Requirement: Memory and knowledge inspector tabs are separate
The right-side status inspector SHALL expose separate Memory and Knowledge tabs with domain-specific fields.

#### Scenario: Memory tab renders memory context details
- **WHEN** the selected turn has `memory_context`
- **THEN** the Memory tab displays memory status, item count, scope, relevance or confidence when present, source, TTL when present, errors, and raw JSON details

#### Scenario: Knowledge tab renders knowledge context details
- **WHEN** the selected turn has `knowledge_context`
- **THEN** the Knowledge tab displays knowledge status, item count, source IDs, item scores, titles, URIs, citations, denied source IDs, errors, and raw JSON details

#### Scenario: Knowledge tab does not merge memory items
- **WHEN** both memory and knowledge contexts exist for the selected turn
- **THEN** memory items appear only in the Memory tab and knowledge items appear only in the Knowledge tab
