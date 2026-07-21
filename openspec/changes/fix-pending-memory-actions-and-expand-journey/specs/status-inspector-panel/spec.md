## ADDED Requirements

### Requirement: Memory inspector exposes valid pending decision actions
The Memory tab SHALL show management actions for unresolved Formation Decisions according to the existing UPDATE/DELETE confirmation policy and SHALL explain missing associations instead of silently hiding the action area.

#### Scenario: Pending UPDATE is fully associated
- **WHEN** a Formation Decision has `decision_status=pending`, a safe `decision_id`, and `proposed_operation=update`
- **THEN** the Memory tab shows both Confirm and Reject actions and sends the existing owner identity, idempotency key, reason, and expected revision when activated

#### Scenario: Pending DELETE is fully associated
- **WHEN** a Formation Decision has `decision_status=pending`, a safe `decision_id`, and `proposed_operation=delete`
- **THEN** the Memory tab shows Confirm and Reject actions and requires the existing destructive-action confirmation before sending Confirm

#### Scenario: Pending ADD keeps the existing route
- **WHEN** a pending decision has a safe `decision_id` but is not an eligible UPDATE or DELETE
- **THEN** the Memory tab shows Reject, does not show Confirm, and does not reinterpret the decision as UPDATE or DELETE

#### Scenario: Pending decision association is incomplete
- **WHEN** the trace reports `decision_status=pending` but does not contain a safe `decision_id`
- **THEN** the Memory tab displays an explicit association-data error and a refresh path rather than hiding the decision controls without explanation

#### Scenario: Pending action completes
- **WHEN** Confirm or Reject succeeds
- **THEN** the inspector refreshes the selected turn's Memory Trace and replaces the unresolved action state with the returned resolution and provider/index status

#### Scenario: Recall provider times out while a decision is actionable
- **WHEN** the selected turn contains a Recall `provider_timeout` and an independently actionable pending Formation Decision
- **THEN** the inspector displays both conditions in their respective sections and does not hide or disable the Formation management actions because Recall failed

### Requirement: Routing journey expands memory formation stages
The routing journey SHALL keep one concise top-level memory-formation node and provide an expandable child flow derived only from the selected turn's existing Memory Request Trace and Formation Traces.

#### Scenario: Formation subflow is expanded
- **WHEN** a user expands the memory-formation node after trace data is available
- **THEN** the journey shows bounded stages for turn capture, background delivery, candidate formation, semantic/policy decision, optional human handling, canonical memory update, and retrieval index update

#### Scenario: Request has not returned
- **WHEN** the route or route-and-invoke request is still pending
- **THEN** the memory child stages remain waiting and MUST NOT advance using fixed timers or inferred internal progress

#### Scenario: Formation job is running or retrying
- **WHEN** the selected turn reports a pending, claimed, or retrying Formation Job
- **THEN** the corresponding background or candidate stage is active while completed earlier stages remain completed

#### Scenario: Decision requires human handling
- **WHEN** an unresolved Formation Decision has `decision_status=pending`
- **THEN** the decision stage uses a distinct attention state, the parent node summarizes the number of changes requiring action, and the UI does not describe the decision as still automatically running

#### Scenario: Pending decision references an existing ready memory
- **WHEN** a pending decision contains memory, revision, or ready index references from the current memory it conflicts with
- **THEN** the journey does not treat those references alone as proof that the pending candidate was persisted or indexed

#### Scenario: Accepted write is persisted and indexed
- **WHEN** an accepted ADD, UPDATE, or DELETE decision reaches its canonical and provider/index terminal state
- **THEN** the corresponding memory-update and index stages show completed using that decision's actual lifecycle outcome

#### Scenario: Job completes with no write
- **WHEN** Formation completes with no candidates or only NOOP/REJECT decisions
- **THEN** the decision stage shows the truthful terminal outcome and the memory-update and index stages are skipped rather than failed

#### Scenario: Formation or indexing fails
- **WHEN** the selected trace reaches a formation or index retry, dead-letter, or failed stage
- **THEN** the affected child stage shows active retry or failed status without changing the completed route, Agent, or response stages

#### Scenario: Recall provider reports an error
- **WHEN** `memoryContext.errors` contains a provider timeout or other safe Recall error
- **THEN** the error remains associated with the earlier reference-preparation stage and is not presented as a failure of the post-response Formation subflow

#### Scenario: Narrow viewport displays expanded stages
- **WHEN** the memory subflow is expanded in a narrow stacked layout
- **THEN** its labels, statuses, and controls fit the available width without horizontal page scrolling or text overlap

