## ADDED Requirements

### Requirement: Hard budget applies to final Context Projection
The system SHALL enforce the consumer hard budget against the final rendered Context Projection, including textual wrappers and serialized structured values.

#### Scenario: Structured value exceeds budget
- **WHEN** a Context Item has bounded text but an unbounded structured value
- **THEN** the structured value is truncated, projected to allowed fields, replaced by a reference, or dropped before consumer input is built

#### Scenario: Must-include item exceeds budget
- **WHEN** an item is marked must-include but its inclusion would exceed the hard budget
- **THEN** must-include affects selection priority only
- **AND** the system applies a bounded representation or returns a controlled budget-exhausted outcome

#### Scenario: Final projection stays within budget
- **WHEN** context assembly succeeds
- **THEN** the estimated token cost of the final Context Projection does not exceed its computed hard context budget

### Requirement: Context budget reserves non-context model capacity
The system SHALL derive available context capacity after reserving space for system instructions, response schema and routing rules, candidate Agent data, and model output.

#### Scenario: Candidate Agent payload grows
- **WHEN** the access-filtered candidate Agent payload consumes more of the configured model window
- **THEN** the available Context Projection budget is reduced rather than allowing the total prompt estimate to exceed the configured window

#### Scenario: Model window information is unavailable
- **WHEN** no exact model window or tokenizer is configured
- **THEN** the system uses conservative configured reservations and approximate token conversion
- **AND** records that usage as estimated

## MODIFIED Requirements

### Requirement: Context selection respects priority and relevance
The system SHALL select eligible Context Items according to authority, permission, visibility, freshness, conflict outcome, deduplication, priority, relevance, recency, and the final Projection budget.

#### Scenario: Budget has enough space
- **WHEN** all eligible rendered Context Items fit within the final budget
- **THEN** the Context Pack includes all eligible items

#### Scenario: Budget is exceeded
- **WHEN** rendered candidate items exceed the available budget
- **THEN** lower-authority, lower-priority, lower-relevance, older, or redundant items are excluded before critical current-state items

#### Scenario: Agent chat transcript exceeds budget
- **WHEN** router-recorded user inputs and host-written child Agent replies in Agent history exceed the available history budget
- **THEN** older or lower-priority transcript items are trimmed or dropped before current input, current Agent state, active Plan state, or explicit task-critical results

#### Scenario: Permission boundary
- **WHEN** a Context Item belongs to a subject, source, purpose, or consumer unavailable to the current user or caller
- **THEN** the item is removed before ranking and MUST NOT be included in the Context Pack or Projection

#### Scenario: Duplicate candidates exist
- **WHEN** multiple candidates represent the same result, event, artifact, memory, or fact
- **THEN** duplicate representations are removed before consuming budget

### Requirement: Trimming records reasons
The system SHALL record why Context Candidates are included, truncated, referenced, dropped, duplicated, overridden, or rejected, while keeping raw diagnostic content bounded and redacted.

#### Scenario: Item dropped by budget
- **WHEN** a Context Item is excluded because the final Projection budget is exhausted
- **THEN** Context Trace records a budget-related drop reason

#### Scenario: Item truncated
- **WHEN** textual or structured Context Item content exceeds a per-item limit
- **THEN** the included representation records that it was truncated and which limit applied

#### Scenario: Summary placeholder recorded
- **WHEN** a Context Item is too long for full inclusion
- **THEN** it may use a deterministic summary placeholder or stable source reference with the original source metadata

#### Scenario: LLM summarization excluded
- **WHEN** Context Pipeline trimming runs in this change
- **THEN** it MUST NOT call an additional LLM solely to summarize context content

#### Scenario: Duplicate or overridden item recorded
- **WHEN** a Candidate is removed by deduplication or current-turn authority
- **THEN** Trace records `duplicate_dropped`, `overridden_for_turn`, or an equivalent deterministic reason

### Requirement: Evidence and memory use shared budget path
Evidence, Memory, and Knowledge Items SHALL enter Router or Agent consumer input only through the shared Context Pipeline budget and Projection path.

#### Scenario: Evidence exceeds evidence allocation
- **WHEN** Evidence items exceed the configured route-stage Evidence budget
- **THEN** lower-value Evidence items are dropped, truncated, or referenced before Router Projection

#### Scenario: Memory exceeds memory allocation
- **WHEN** recalled Memory items exceed the configured Router or Agent Memory budget
- **THEN** lower-value Memory items are dropped or truncated before the relevant consumer Projection

#### Scenario: Knowledge exceeds knowledge allocation
- **WHEN** Knowledge items and citations exceed the configured Router or Agent Knowledge budget
- **THEN** lower-value items are dropped, truncated, or represented by bounded references before Projection
