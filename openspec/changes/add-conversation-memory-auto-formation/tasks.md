## 1. Baseline And Contracts

- [ ] 1.1 Record the current backend/frontend test baseline and verify existing explicit memory write, mem0 recall, governed context, and debug-console tests before changing behavior
- [ ] 1.2 Add formation enums and schemas for trigger, mode, job status, operation, decision status, lifecycle status, index status, and reason codes
- [ ] 1.3 Add strict schemas for `MemoryFormationTurn`, `MemoryFormationJob`, `MemoryFormationCandidate`, evidence refs, lifecycle operation, and `MemoryFormationTrace`
- [ ] 1.4 Extend memory item/write decision schemas with memory key, candidate hash, current revision, formation job, lifecycle, index, and canonical refs while preserving existing API compatibility
- [ ] 1.5 Add configuration for formation mode, five-turn window, thirty-second idle, model/prompt version, confidence thresholds, timeouts, lease/retry/dead-letter, sweeper, consolidation, and bounded capsule content
- [ ] 1.6 Expose only non-sensitive formation mode/version/worker/queue health fields in runtime configuration
- [ ] 1.7 Add schema/config validation tests for defaults, overrides, invalid threshold ordering, invalid lease/retry values, and secret redaction
- [ ] 1.8 Extend the existing `Plan` schema with required non-empty `user_id` and `tenant_id`, and add tests proving raw LLM/Host ownership is overwritten from trusted UserContext

## 2. PostgreSQL Schema And Migration

- [ ] 2.1 Add failing model/schema tests for memory revisions, formation turns/jobs, lifecycle/index state, uniqueness, and round-trip serialization
- [ ] 2.2 Add `memory_revisions` ORM/SQL schema with revision ordering, superseded relation, evidence refs, policy version, content, structured value, and timestamps
- [ ] 2.3 Add `memory_formation_turns` ORM/SQL schema with bounded capsule data, source refs, used memory IDs, idle deadline, claim/status, and timestamps
- [ ] 2.4 Add `memory_formation_jobs` ORM/SQL schema with trigger, frozen turn range, idempotency key, mode/version, lease, attempts, retry/dead-letter, trace summary, and timestamps
- [ ] 2.5 Add durable mem0 index/delete operation outbox fields or table with operation, OIR/external IDs, revision, status, attempts, lease, and bounded error metadata
- [ ] 2.6 Extend `memory_items` with memory key, current revision ID, lifecycle status, index status, and formation job ID
- [ ] 2.7 Add tenant/subject/scope/memory-key active uniqueness and job idempotency indexes plus pending/idle/lease/sweeper query indexes
- [ ] 2.8 Add non-destructive migration/backfill logic that creates revision 1 and deterministic legacy memory keys for existing active memory items while preserving OIR/mem0 IDs
- [ ] 2.9 Add SQLite test-schema compatibility and PostgreSQL migration tests for memory tables, including rerun/idempotency and rollback-safe nullable memory lifecycle deployment
- [ ] 2.10 Make `plans.user_id` NOT NULL, add NOT NULL indexed `plans.tenant_id`, and update development schema setup to reject or clear old unowned Plan data without a compatibility/backfill path

## 3. Repository Layer

- [ ] 3.1 Define repository protocols for formation turns/jobs, memory revisions, lifecycle compare-and-set, index operations, and filtered trace queries
- [ ] 3.2 Implement in-memory formation turn/job repositories with frozen ranges, successful watermark, idle deadline, lease, retry, and dead-letter semantics
- [ ] 3.3 Implement database formation turn/job repositories using transactions, uniqueness, row locking or skip-locked equivalents, and lease expiry recovery
- [ ] 3.4 Implement in-memory and database revision repositories with atomic revision numbering, superseded links, and current pointer updates
- [ ] 3.5 Extend memory item repositories for lookup by tenant/subject/scope/memory key, lifecycle filtering, index status, deletion pending, and canonical active-ID validation
- [ ] 3.6 Implement index/delete outbox repositories with claim, complete, retry, dead-letter, and repair queries
- [ ] 3.7 Extend memory event/debug repositories with request/session/turn/run/job/memory-key/decision filters without unbounded client-side tenant filtering
- [ ] 3.8 Add repository concurrency, uniqueness, watermark, lease-expiry, revision-sequencing, deletion-pending, and cross-tenant isolation tests
- [ ] 3.9 Update in-memory/database Plan repositories so save persists required ownership and get/get-active queries require tenant/user filters

## 4. Turn Capture And Trigger Coordination

- [ ] 4.1 Add Turn Capsule builder tests for successful, failed, invalid-output, artifact-heavy, memory-context-heavy, and sensitive requests
- [ ] 4.2 Implement bounded Turn Capsule construction from user input, Agent result message/output summary, run/result/plan/artifact refs, and actual used memory IDs
- [ ] 4.3 Prevent request-level temporary/private turns from entering formation storage and record only a redacted skipped trace
- [ ] 4.4 Implement `FormationTriggerCoordinator` append-and-check transaction for pending turns, five-turn frozen ranges, and idle deadline reset
- [ ] 4.5 Implement durable idle sweeper that creates jobs for expired deadlines without requiring Host session-close events
- [ ] 4.6 Implement job claim/lease/retry/dead-letter worker orchestration and graceful startup/shutdown through application lifespan
- [ ] 4.7 Ensure new turns arriving during an active job start the next range and failed jobs do not advance the successful watermark
- [ ] 4.8 Add deterministic idempotency keys for tenant/session/turn-range/policy-version and structured-event source/version
- [ ] 4.9 Add race tests for fifth-turn versus idle, duplicate worker claims, lease expiry, service restart, retry replay, and new turns during active jobs
- [ ] 4.10 Add rollout tests proving off creates no automatic side effects, observe records decisions only, and enforced executes lifecycle operations

## 5. Formation Model And Structured Projectors

- [ ] 5.1 Define `ConversationFormationModel` protocol and a fake implementation for deterministic tests
- [ ] 5.2 Implement the OpenAI-compatible formation model adapter with separate model/prompt/version/timeout configuration and strict JSON response validation
- [ ] 5.3 Create the bounded formation prompt covering scopes, evidence, temporary-versus-long-term language, sensitive exclusions, old-recall self-reinforcement, operations, and output schema
- [ ] 5.4 Add parser/error handling so invalid JSON, partial candidates, timeout, and provider errors produce retryable job outcomes with no partial lifecycle side effects
- [ ] 5.5 Implement `StructuredEventProjector` for Plan create/update/confirm/cancel and current/next step status
- [ ] 5.6 Extend structured projection for Run/Result completion/failure and normalized Artifact refs using canonical IDs and bounded summaries
- [ ] 5.7 Ensure optional model summaries cannot override canonical object IDs, statuses, actor, tenant, or relationships
- [ ] 5.8 Add multilingual/natural-language tests for remember, forget, future preference, current-turn override, indirect phrasing, ambiguous subject, and unrelated conversational text without keyword shortcuts
- [ ] 5.9 Add structured projector tests for event replay, status progression, terminal Plan, bounded Result/Artifact projection, and canonical authority
- [ ] 5.10 Require StructuredEventProjector to inherit stored Plan tenant/user ownership, generate tenant-user-plan memory keys, and reject malformed unowned projections

## 6. Deterministic Candidate Policy

- [ ] 6.1 Add candidate-policy tests for evidence authority, used-memory self-repetition, subject reconstruction, tenant isolation, scope allowlist, and DLP outcomes
- [ ] 6.2 Implement trusted identity reconstruction from job/request/canonical event and reject model-proposed cross-subject or cross-tenant targets
- [ ] 6.3 Implement bounded evidence validation and prohibit Assistant-only evidence from proving user stable facts
- [ ] 6.4 Implement deterministic DLP/sensitivity checks for credentials, secrets, authorization data, regulated identifiers, and third-party privacy before persistence/display
- [ ] 6.5 Implement deterministic memory-key builders for preference/fact slots and Plan/Run/Result/Artifact canonical projections
- [ ] 6.6 Implement candidate hash and operation idempotency checks before semantic comparison
- [ ] 6.7 Implement configurable default decision thresholds: automatic at or above 0.90, pending from 0.70 to below 0.90, and reject below 0.70
- [ ] 6.8 Implement same-key/same-value NOOP, explicit long-term UPDATE, current-turn override without UPDATE, and ambiguous conflict PENDING
- [ ] 6.9 Implement DELETE guard requiring authorized explicit-user evidence or deterministic lifecycle reason plus a unique current target
- [ ] 6.10 Add policy decision reason-code and redacted trace tests for every ADD/UPDATE/DELETE/NOOP/REJECT/PENDING branch

## 7. Revision And Lifecycle Service

- [ ] 7.1 Add lifecycle service tests for ADD revision 1, stable-ID UPDATE, superseded links, concurrent update ordering, and current projection uniqueness
- [ ] 7.2 Implement canonical ADD transaction for current item, revision, event, formation decision, and pending index operation
- [ ] 7.3 Implement canonical UPDATE transaction with row lock/compare-and-set, incremented revision, superseded relation, current pointer, and pending index operation
- [ ] 7.4 Implement NOOP/REJECT/PENDING persistence with bounded reasons and no provider side effects
- [ ] 7.5 Implement authorized deletion request resolution, lifecycle=`deletion_pending`, immediate recall exclusion, idempotent delete operation, and bounded audit event
- [ ] 7.6 Complete hard deletion after provider success by deleting current/revision content and writing a content-free tombstone
- [ ] 7.7 Keep failed provider deletion fail-closed and retryable, retain only required external mapping, and expose dead-letter state without reactivating memory
- [ ] 7.8 Extend TTL recall filtering and implement TTL sweeper using the same hard-delete lifecycle operation
- [ ] 7.9 Ensure user/TTL deletion removes directly associated pending capsule/job content and invalidates frontend/debug caches where applicable
- [ ] 7.10 Implement tenant-partitioned consolidation for exact/semantic duplicates, session summaries, stale task projections, and conflict pending through normal policy/revision paths
- [ ] 7.11 Add lifecycle tests for ambiguous/repeated delete, mem0 failure, TTL race, tombstone redaction, consolidation revision, and cross-tenant prohibition

## 8. mem0 Adapter And Index Consistency

- [ ] 8.1 Add fake mem0 tests proving every governed ADD passes one canonical content item with `infer=False` and never passes the full Turn Capsule message list
- [ ] 8.2 Extend `MemoryStrategyAdapter` and `Mem0MemoryAdapter` with update and index-operation result contracts while preserving repository fallback compatibility
- [ ] 8.3 Change explicit and automatic accepted ADD paths to pass `infer=False`, complete metadata, and stable OIR memory/revision IDs
- [ ] 8.4 Implement mem0 update by known external ID and metadata refresh without creating a second logical OIR memory
- [ ] 8.5 Strengthen delete handling for not-found idempotency, retryable provider errors, external ID mapping, and deletion status events
- [ ] 8.6 Validate mem0 search results against canonical active/lifecycle/TTL/tenant/subject state before building `memory_context`
- [ ] 8.7 Implement add-crash recovery/adoption by OIR memory ID metadata and detect duplicate/orphan provider records
- [ ] 8.8 Implement index repair/reindex service for missing mappings, out-of-sync revisions, duplicate/orphan vectors, and full rebuild from active PostgreSQL projections
- [ ] 8.9 Add mem0 degraded/fail-closed tests for add/update/delete/search/repair and verify provider success is never reported on fallback/error
- [ ] 8.10 Extend the real PostgreSQL + mem0 2.0.11 + Milvus Lite smoke helper to verify infer-false single vector, update-in-place, hard delete, and rebuild

## 9. Invocation, Event, Plan, And Context Integration

- [ ] 9.1 Inject turn-capture/trigger dependencies into invocation services without coupling invokers to memory implementation details
- [ ] 9.2 Hook Turn Capsule persistence after AgentRun/AgentResult persistence for direct invoke and route-and-invoke, preserving main response latency and status
- [ ] 9.3 Publish structured formation commands after Plan save/confirm/cancel and Agent event-driven Plan transitions
- [ ] 9.4 Publish structured formation commands after Run/Result/Artifact state is durably persisted and make duplicate events idempotent
- [ ] 9.5 Project active task memory with derived authority and canonical plan/current-step refs into the existing governed route/agent context provider path
- [ ] 9.6 Ensure current input and canonical active Plan override stale task memory, and Agent execution reloads Plan by ID before continuing
- [ ] 9.7 Add integration tests for “继续上次任务” resolving active Plan without a new intent category and unrelated new tasks ignoring old task memory
- [ ] 9.8 Add integration tests proving route-only/incomplete turns, failed invocations, duplicate events, and feature-off modes do not produce unintended long-term memory
- [ ] 9.9 Bind and overwrite required Plan user/tenant ownership from trusted UserContext before final route Plan validation and persistence, including direct Plan creation/execution paths
- [ ] 9.10 Enforce stored Plan ownership for get, active lookup, confirm, cancel, execute, Agent-event update, task projection, and continuation context

## 10. Debug, Management API, And Metrics

- [ ] 10.1 Extend memory debug response schemas and endpoints with authorized request/session/turn/run/job/memory-key/decision filters
- [ ] 10.2 Add bounded Formation Trace assembly for trigger, source range, versions, attempts, decisions, revisions, provider/index status, latency, and usage
- [ ] 10.3 Keep Context Trace and Formation Trace separately modeled and linked by stable refs without adding debug payloads to Router/Agent model inputs
- [ ] 10.4 Add user-owned memory lifecycle endpoints for delete, pending confirm/reject, operation status, and idempotency/precondition handling
- [ ] 10.5 Add admin cross-subject management authorization, actor/reason audit, and non-disclosing authorization failures
- [ ] 10.6 Redact candidate quotes, formation prompts, secrets, regulated values, provider credentials, tombstones, and errors in events/API/logs
- [ ] 10.7 Add runtime/admin views for worker state, queue depth, oldest pending age, dead-letter, index out-of-sync, and last safe error
- [ ] 10.8 Add metrics for job/model latency and usage, decision rates, retries/dead-letter, pending age, index repair, deletion completion, and recall use without content labels
- [ ] 10.9 Add API/auth/redaction tests for every filter and lifecycle management operation, including cross-tenant denial

## 11. Conversation Console

- [ ] 11.1 Extend frontend types and per-turn state for actual Recall Used, formation job refs, asynchronous decisions, lifecycle/index status, and pending operations
- [ ] 11.2 Add compact assistant-turn badges for Recall Used and formation ADD/UPDATE/DELETE/NOOP/REJECT/PENDING counts without placing technical details in chat text
- [ ] 11.3 Split the selected-turn Memory inspector into Recall Used and Formation / Write Decisions with stable loading/empty/pending/error states
- [ ] 11.4 Fetch or subscribe to turn/job formation status after the original response and associate one multi-turn job without duplicating decisions
- [ ] 11.5 Ensure selecting an older turn uses only its trace and never derives recall/write counts from global Memory Debug state
- [ ] 11.6 Add bounded decision details and redact sensitive rejected content in previews and raw JSON views
- [ ] 11.7 Add authorized confirm/reject/delete controls with destructive confirmation, idempotency/precondition payload, and pending/completed/conflict feedback
- [ ] 11.8 Preserve the global Memory Debug management view as a distinct repository-level surface and show formation filters/status without conflating it with selected-turn trace
- [ ] 11.9 Add frontend unit/E2E tests for delayed idle updates, five-turn ranges, older-turn selection, pending controls, denied operations, errors, and redaction

## 12. End-To-End Verification And Rollout

- [ ] 12.1 Add backend unit tests for all formation/job/policy/lifecycle reason codes and target at least one scenario per OpenSpec requirement
- [ ] 12.2 Add database integration tests for migrations, restart recovery, multi-worker races, revision consistency, deletion pending, and outbox repair
- [ ] 12.3 Add service integration tests covering five turns, thirty-second idle, no session close, structured immediate projection, main-response independence, and rollout modes
- [ ] 12.4 Add end-to-end isolation tests proving tenant/user/subject separation across formation, dedupe, consolidation, debug, management, and recall
- [ ] 12.5 Add privacy tests proving temporary mode skips buffering and user/TTL deletion removes current/revision/provider data plus directly associated pending payload content
- [ ] 12.6 Run the full backend and frontend regression suites and resolve compatibility regressions in explicit write-candidates, existing memory_context, context pipeline, and debug console
- [ ] 12.7 Run and document real infrastructure smoke for preference ADD/recall/UPDATE/delete and unfinished Plan continuation versus unrelated new task
- [ ] 12.8 Update `.env.example`, runtime/config docs, mem0 integration docs, Debug/E2E test plan, and the strategy document with final implemented contracts without exposing credentials
- [ ] 12.9 Deploy schema/services with mode off, validate backfill, run observe-mode quality review, then enable enforced for an isolated tenant
- [ ] 12.10 Define rollout gates for queue/dead-letter/index/delete health, formation precision/correction/NOOP rates, cost/latency, and an emergency mode-off rollback drill
- [ ] 12.11 Add Plan ownership E2E tests for missing tenant/user rejection, forged ownership overwrite, same-tenant cross-user denial, event ownership preservation, and owned continuation
