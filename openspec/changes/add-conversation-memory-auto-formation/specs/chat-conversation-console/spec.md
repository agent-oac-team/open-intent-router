## ADDED Requirements

### Requirement: Conversation turn 展示实际 Recall Used
The visual test UI SHALL retain and display the memory items actually included in Router or Agent projections for each selected conversation turn, rather than inferring use from repository debug state.

#### Scenario: Turn uses recalled memory
- **WHEN** a completed turn trace identifies memory items included for Router, Agent, or both consumers
- **THEN** the assistant turn shows a compact `Recall used` count and the selected-turn Memory inspector shows memory ID, revision, scope, consumer, relevance/confidence, source and canonical refs

#### Scenario: Recalled item is overridden or dropped
- **WHEN** memory search returned an item but governed context excluded or overrode it
- **THEN** the inspector distinguishes considered/dropped/overridden from actually used and does not count it as Recall Used

#### Scenario: Older turn is selected
- **WHEN** the developer selects an older conversation turn
- **THEN** Recall Used shows that turn's trace and MUST NOT reuse the latest turn or current global Memory Items

### Requirement: Conversation turn 支持异步 Formation Decisions
The visual test UI SHALL associate formation jobs and write decisions with their source turn range even when those decisions complete after the original route-and-invoke response.

#### Scenario: Fifth turn queues formation
- **WHEN** a five-turn window creates a formation job
- **THEN** the UI shows pending formation status on the source range or the fifth turn without blocking or rewriting the assistant message

#### Scenario: Idle formation completes later
- **WHEN** an idle job completes after the conversation response
- **THEN** polling, push, or refresh updates the associated turn range with ADD/UPDATE/DELETE/NOOP/REJECT/PENDING counts and final job status

#### Scenario: Formation job spans multiple turns
- **WHEN** one job uses multiple source turns
- **THEN** the inspector exposes the full source range and allows each participating turn to navigate to the same job without duplicating lifecycle decisions

#### Scenario: Formation fails or retries
- **WHEN** a formation job is retrying, dead-lettered, or has an index error
- **THEN** the selected-turn Memory inspector shows the bounded status/error and MUST NOT mark the memory as successfully written

### Requirement: Memory inspector 分离 Recall 和 Formation 区域
The selected-turn Memory inspector SHALL present `Recall Used` and `Formation / Write Decisions` as separate sections with stable empty, loading, pending, success, and error states.

#### Scenario: Turn has recall but no formation result yet
- **WHEN** recall trace is available and formation is pending or not triggered
- **THEN** Recall Used renders normally while Formation shows pending/not-triggered instead of an empty recall state

#### Scenario: Turn has formation but no recalled memory
- **WHEN** no memory was used for the turn but the turn participates in a formation job
- **THEN** the inspector shows an empty Recall Used section and the actual Formation Decisions

#### Scenario: Decision details are displayed
- **WHEN** a formation decision is selected
- **THEN** the UI shows operation/status, scope, bounded content preview when allowed, reason, memory key/ID, revision, trigger, source refs and provider/index state

#### Scenario: Sensitive rejection is displayed
- **WHEN** a candidate was rejected as sensitive
- **THEN** the UI shows the policy reason and stable refs but MUST NOT show the rejected secret or regulated content

### Requirement: Pending decisions 提供受控用户操作
The selected-turn Memory inspector SHALL allow an authorized user to confirm or reject eligible PENDING UPDATE/DELETE decisions and to request deletion of an owned memory through management APIs distinct from read-only debug APIs.

#### Scenario: User confirms pending update
- **WHEN** the selected pending UPDATE is still valid and the user confirms it
- **THEN** the UI submits the decision ID and idempotency/precondition data, then displays pending/completed/conflict lifecycle status

#### Scenario: User rejects pending delete
- **WHEN** the user rejects a pending DELETE
- **THEN** the UI marks the decision resolved after backend confirmation and leaves current memory active

#### Scenario: User requests memory deletion
- **WHEN** the user invokes delete for an owned memory
- **THEN** the UI requires a clear destructive confirmation, calls the authorized lifecycle endpoint, and displays deletion pending until provider deletion completes

#### Scenario: User lacks permission
- **WHEN** the management API rejects an operation for ownership or authorization reasons
- **THEN** the UI shows a bounded denial and does not reveal additional target memory details

### Requirement: 全局 Memory Debug 不得冒充本轮 trace
The frontend SHALL keep global Memory Debug repository state separate from selected-turn Recall Used and Formation Decisions.

#### Scenario: Global debug contains unrelated memory
- **WHEN** `/memories/debug` returns items or events not linked to the selected turn/job
- **THEN** they remain visible only in the management/debug view and MUST NOT appear in that turn's Recall or Formation counts

#### Scenario: Selected turn trace API is unavailable
- **WHEN** the turn-specific formation trace cannot be loaded
- **THEN** the UI shows unavailable/error for that section instead of deriving decisions from global latest events

