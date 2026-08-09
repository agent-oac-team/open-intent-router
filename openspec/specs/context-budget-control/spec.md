# context-budget-control Specification

## Purpose
TBD - created by archiving change m4-context-pack. Update Purpose after archive.
## Requirements
### Requirement: Context budget uses token units by default
The system SHALL use token budget as the default unit for model-bound Context Pack limits.

#### Scenario: Default budget applied
- **WHEN** a route request does not provide a request-specific context budget
- **THEN** the Context Pack uses the configured default token budget

#### Scenario: Request budget override
- **WHEN** a route request or frontend context provides an allowed request-specific context budget
- **THEN** the Context Pack uses that budget without exceeding configured safety limits

### Requirement: Character limits can be converted to token budgets
The system SHALL support approximate conversion between character limits and token budgets.

#### Scenario: Host provides character limit
- **WHEN** a host application provides only a character limit
- **THEN** the backend converts it to an approximate token budget before selecting Context Items

#### Scenario: Provider usage unavailable
- **WHEN** the LLM Provider does not return token usage
- **THEN** the backend estimates token usage from character counts and marks the usage as estimated

#### Scenario: Provider usage available
- **WHEN** the LLM Provider returns token usage
- **THEN** the backend records the returned token usage as the preferred usage source

### Requirement: Context selection respects priority and relevance
The system SHALL select Context Items according to priority, relevance, recency, permissions, and budget.

#### Scenario: Budget has enough space
- **WHEN** all candidate Context Items fit within the budget
- **THEN** the Context Pack includes all eligible items

#### Scenario: Budget is exceeded
- **WHEN** candidate Context Items exceed the available budget
- **THEN** lower-priority or lower-relevance items are excluded before critical current-state items

#### Scenario: Agent chat transcript exceeds budget
- **WHEN** router-recorded user inputs and host-written child Agent replies in Agent history exceed the available history budget
- **THEN** older or lower-priority transcript items are trimmed or dropped before current input, current Agent state, active Plan state, or explicit task-critical results

#### Scenario: Permission boundary
- **WHEN** a Context Item belongs to a subject or source unavailable to the current user
- **THEN** the item MUST NOT be included in the Context Pack

### Requirement: Trimming records reasons
The system SHALL record why Context Items are included, trimmed, dropped, or compressed.

#### Scenario: Item dropped by budget
- **WHEN** a Context Item is excluded because the budget is exhausted
- **THEN** the item or usage summary records a budget-related drop reason

#### Scenario: Item truncated
- **WHEN** a Context Item content exceeds the per-item limit
- **THEN** the included item records that it was truncated

#### Scenario: Summary placeholder recorded
- **WHEN** a Context Item is too long for full inclusion in the first M4 implementation
- **THEN** the item may record a summary placeholder and source reference metadata without invoking an additional LLM summarization step

#### Scenario: LLM summarization excluded
- **WHEN** Context Pack trimming runs in M4
- **THEN** it MUST NOT call an additional LLM solely to summarize context content

### Requirement: Evidence and memory use shared budget path
Evidence and future memory items SHALL enter the model only through the Context Pack budget path.

#### Scenario: Evidence exceeds evidence allocation
- **WHEN** Evidence items exceed the configured or derived evidence budget
- **THEN** lower-value Evidence items are dropped or trimmed before entering the prompt

#### Scenario: Future memory items
- **WHEN** Memory recall is added by a later module
- **THEN** memory items are subject to Context Pack priority and budget rules before entering the prompt

### Requirement: Tokenizer dependency is optional
The system SHALL NOT require a model-specific tokenizer dependency for the first Context Pack implementation.

#### Scenario: No tokenizer installed
- **WHEN** no model-specific tokenizer is configured
- **THEN** Context Pack token estimates use the configured approximate conversion strategy

#### Scenario: Tokenizer added later
- **WHEN** a later implementation adds tokenizer-based estimation
- **THEN** the Context Budget contract remains compatible with token estimates and usage source metadata

