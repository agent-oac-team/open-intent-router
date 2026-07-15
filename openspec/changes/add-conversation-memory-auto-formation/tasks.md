## 1. Baseline And Contracts

- [x] 1.1 Record the current backend/frontend test baseline and verify existing explicit memory write, mem0 recall, governed context, and debug-console tests before changing behavior
  - 2026-07-13 baseline: `.venv/bin/python -m pytest` -> 137 passed, 1 Starlette deprecation warning; `cd web && npm run test` -> 9 passed. The backend suite includes explicit memory write/mem0 recall and governed context coverage; the frontend suite includes selected-turn trace and read-only Memory Debug coverage.
- [x] 1.2 Add formation enums and schemas for trigger, mode, job status, operation, decision status, lifecycle status, index status, and reason codes
- [x] 1.3 Add strict schemas for `MemoryFormationTurn`, `MemoryFormationJob`, `MemoryFormationCandidate`, evidence refs, lifecycle operation, and `MemoryFormationTrace`
- [x] 1.4 Extend memory item/write decision schemas with memory key, candidate hash, current revision, formation job, lifecycle, index, and canonical refs while preserving existing API compatibility
- [x] 1.5 Add configuration for formation mode, five-turn window, thirty-second idle, model/prompt version, confidence thresholds, timeouts, lease/retry/dead-letter, sweeper, consolidation, and bounded capsule content
- [x] 1.6 Expose only non-sensitive formation mode/version/worker/queue health fields in runtime configuration
- [x] 1.7 Add schema/config validation tests for defaults, overrides, invalid threshold ordering, invalid lease/retry values, and secret redaction
- [x] 1.8 Extend the existing `Plan` schema with required non-empty `user_id` and `tenant_id`, and add tests proving raw LLM/Host ownership is overwritten from trusted UserContext

## 2. PostgreSQL Schema And Migration

- [x] 2.1 Add failing model/schema tests for memory revisions, formation turns/jobs, lifecycle/index state, uniqueness, and round-trip serialization
- [x] 2.2 Add `memory_revisions` ORM/SQL schema with revision ordering, superseded relation, evidence refs, policy version, content, structured value, and timestamps
- [x] 2.3 Add `memory_formation_turns` ORM/SQL schema with bounded capsule data, source refs, used memory IDs, idle deadline, claim/status, and timestamps
- [x] 2.4 Add `memory_formation_jobs` ORM/SQL schema with trigger, frozen turn range, idempotency key, mode/version, lease, attempts, retry/dead-letter, trace summary, and timestamps
- [x] 2.5 Add durable mem0 index/delete operation outbox fields or table with operation, OIR/external IDs, revision, status, attempts, lease, and bounded error metadata
- [x] 2.6 Extend `memory_items` with memory key, current revision ID, lifecycle status, index status, and formation job ID
- [x] 2.7 Add tenant/subject/scope/memory-key active uniqueness and job idempotency indexes plus pending/idle/lease/sweeper query indexes
- [x] 2.8 Add non-destructive migration/backfill logic that creates revision 1 and deterministic legacy memory keys for existing active memory items while preserving OIR/mem0 IDs
- [x] 2.9 Add SQLite test-schema compatibility and PostgreSQL migration tests for memory tables, including rerun/idempotency and rollback-safe nullable memory lifecycle deployment
  - 2026-07-14 verification: SQLite compatibility and legacy backfill coverage passed together with a real PostgreSQL temporary-schema migration test. The PostgreSQL test proves transactional rollback removes the newly created memory tables and lifecycle columns after a forced failure, successful rollout keeps all new lifecycle fields nullable, repeated migration/backfill creates exactly one revision 1 per legacy memory, preserves the existing mem0 mapping as `index_status=ready`, and repairs rows written by an old-version writer from NULL lifecycle fields to `active/pending`. Final gates passed with 10 migration-focused tests, 509 full backend tests, Ruff lint/format, strict OpenSpec validation, and `git diff --check`; only the existing Starlette deprecation warning remains.
  - 2026-07-14 independent review: a separate reviewer approved 2.9 with no blocking or non-blocking findings after checking real PostgreSQL execution, transactional DDL rollback semantics, nullable lifecycle rollout, rerun/idempotency, old-writer NULL recovery, mem0 mapping preservation, temporary-schema cleanup, and SQLite compatibility. The reviewer reran all 10 migration/PostgreSQL tests without a PostgreSQL skip and confirmed no residual test schema.
- [x] 2.10 Make `plans.user_id` NOT NULL, add NOT NULL indexed `plans.tenant_id`, and update development schema setup to reject or clear old unowned Plan data without a compatibility/backfill path

## 3. Repository Layer

- [x] 3.1 Define repository protocols for formation turns/jobs, memory revisions, lifecycle compare-and-set, index operations, and filtered trace queries
- [x] 3.2 Implement in-memory formation turn/job repositories with frozen ranges, successful watermark, idle deadline, lease, retry, and dead-letter semantics
- [x] 3.3 Implement database formation turn/job repositories using transactions, uniqueness, row locking or skip-locked equivalents, and lease expiry recovery
- [x] 3.4 Implement in-memory and database revision repositories with atomic revision numbering, superseded links, and current pointer updates
- [x] 3.5 Extend memory item repositories for lookup by tenant/subject/scope/memory key, lifecycle filtering, index status, deletion pending, and canonical active-ID validation
- [x] 3.6 Implement index/delete outbox repositories with claim, complete, retry, dead-letter, and repair queries
- [x] 3.7 Extend memory event/debug repositories with request/session/turn/run/job/memory-key/decision filters without unbounded client-side tenant filtering
- [x] 3.8 Add repository concurrency, uniqueness, watermark, lease-expiry, revision-sequencing, deletion-pending, and cross-tenant isolation tests
- [x] 3.9 Update in-memory/database Plan repositories so save persists required ownership and get/get-active queries require tenant/user filters
  - 2026-07-13 group review: independent reviewer approved Repository Layer after 50 focused and 195 full backend tests, Ruff lint/format, strict OpenSpec validation, and real PostgreSQL isolation probes for concurrent job creation/claim, revision CAS and owner isolation, structured trace filters, outbox mapping, and immutable Run identity. Only the existing Starlette deprecation warning remains.

## 4. Turn Capture And Trigger Coordination

- [x] 4.1 Add Turn Capsule builder tests for successful, failed, invalid-output, artifact-heavy, memory-context-heavy, and sensitive requests
- [x] 4.2 Implement bounded Turn Capsule construction from user input, Agent result message/output summary, run/result/plan/artifact refs, and actual used memory IDs
- [x] 4.3 Prevent request-level temporary/private turns from entering formation storage and record only a redacted skipped trace
- [x] 4.4 Implement `FormationTriggerCoordinator` append-and-check transaction for pending turns, five-turn frozen ranges, and idle deadline reset
- [x] 4.5 Implement durable idle sweeper that creates jobs for expired deadlines without requiring Host session-close events
- [x] 4.6 Implement job claim/lease/retry/dead-letter worker orchestration and graceful startup/shutdown through application lifespan
- [x] 4.7 Ensure new turns arriving during an active job start the next range and failed jobs do not advance the successful watermark
- [x] 4.8 Add deterministic idempotency keys for tenant/session/turn-range/policy-version and structured-event source/version
- [x] 4.9 Add race tests for fifth-turn versus idle, duplicate worker claims, lease expiry, service restart, retry replay, and new turns during active jobs
- [x] 4.10 Add rollout tests proving off creates no automatic side effects, observe records decisions only, and enforced executes lifecycle operations
  - 2026-07-13 group review: a new independent reviewer approved the complete group after 55 focused and 235 full tests, Ruff/OpenSpec/diff gates, 50 cross-process SQLite fifth-turn/idle races, and real PostgreSQL probes for window/idle convergence, claim/lease recovery, stale-token rejection, watermark/restart recovery, owner-scoped request isolation, and rerunnable legacy constraint migration. Only the existing Starlette deprecation warning remains.

## 5. Formation Model And Structured Projectors

- [x] 5.1 Define `ConversationFormationModel` protocol and a fake implementation for deterministic tests
- [x] 5.2 Implement the OpenAI-compatible formation model adapter with separate model/prompt/version/timeout configuration and strict JSON response validation
- [x] 5.3 Create the bounded formation prompt covering scopes, evidence, temporary-versus-long-term language, sensitive exclusions, old-recall self-reinforcement, operations, and output schema
- [x] 5.4 Add parser/error handling so invalid JSON, partial candidates, timeout, and provider errors produce retryable job outcomes with no partial lifecycle side effects
- [x] 5.5 Implement `StructuredEventProjector` for Plan create/update/confirm/cancel and current/next step status
- [x] 5.6 Extend structured projection for Run/Result completion/failure and normalized Artifact refs using canonical IDs and bounded summaries
- [x] 5.7 Ensure optional model summaries cannot override canonical object IDs, statuses, actor, tenant, or relationships
- [x] 5.8 Add multilingual/natural-language tests for remember, forget, future preference, current-turn override, indirect phrasing, ambiguous subject, and unrelated conversational text without keyword shortcuts
- [x] 5.9 Add structured projector tests for event replay, status progression, terminal Plan, bounded Result/Artifact projection, and canonical authority
- [x] 5.10 Require StructuredEventProjector to inherit stored Plan tenant/user ownership, generate tenant-user-plan memory keys, and reject malformed unowned projections
  - 2026-07-13 group review: a new independent reviewer approved the complete group after three review passes and fixes for worst-case prompt budgeting, streaming response limits, bounded candidate payloads, canonical ID/key limits, URI credential redaction, Plan retrieval semantics, and tenant-scoped candidate identity. Final verification: 67 focused and 275 full backend tests, Ruff lint/format, strict OpenSpec validation, and diff checks passed. Only the existing Starlette deprecation warning remains.

## 6. Deterministic Candidate Policy

- [x] 6.1 Add candidate-policy tests for evidence authority, used-memory self-repetition, subject reconstruction, tenant isolation, scope allowlist, and DLP outcomes
- [x] 6.2 Implement trusted identity reconstruction from job/request/canonical event and reject model-proposed cross-subject or cross-tenant targets
- [x] 6.3 Implement bounded evidence validation and prohibit Assistant-only evidence from proving user stable facts
- [x] 6.4 Implement deterministic DLP/sensitivity checks for credentials, secrets, authorization data, regulated identifiers, and third-party privacy before persistence/display
- [x] 6.5 Implement deterministic memory-key builders for preference/fact slots and Plan/Run/Result/Artifact canonical projections
- [x] 6.6 Implement candidate hash and operation idempotency checks before semantic comparison
- [x] 6.7 Implement configurable default decision thresholds: automatic at or above 0.90, pending from 0.70 to below 0.90, and reject below 0.70
- [x] 6.8 Implement same-key/same-value NOOP, explicit long-term UPDATE, current-turn override without UPDATE, and ambiguous conflict PENDING
- [x] 6.9 Implement DELETE guard requiring authorized explicit-user evidence or deterministic lifecycle reason plus a unique current target
- [x] 6.10 Add policy decision reason-code and redacted trace tests for every ADD/UPDATE/DELETE/NOOP/REJECT/PENDING branch
  - 2026-07-13 group review: the independent candidate-policy reviewer approved the complete group after two review passes and fixes for canonical structured updates, DELETE authority/TTL checks, role-aware stable-fact and response-language evidence, DLP ordering/redaction, natural-key ownership, cross-scope recalled-memory repetition, and structured business descriptions. Final verification: 115 focused and 361 full backend tests, Ruff lint/format, strict OpenSpec validation, diff checks, SQLite expiry/owner probes, and all ADD/UPDATE/DELETE/NOOP/REJECT/PENDING branches passed. Only the existing Starlette deprecation warning remains.

## 7. Revision And Lifecycle Service

- [x] 7.1 Add lifecycle service tests for ADD revision 1, stable-ID UPDATE, superseded links, concurrent update ordering, and current projection uniqueness
- [x] 7.2 Implement canonical ADD transaction for current item, revision, event, formation decision, and pending index operation
- [x] 7.3 Implement canonical UPDATE transaction with row lock/compare-and-set, incremented revision, superseded relation, current pointer, and pending index operation
- [x] 7.4 Implement NOOP/REJECT/PENDING persistence with bounded reasons and no provider side effects
- [x] 7.5 Implement authorized deletion request resolution, lifecycle=`deletion_pending`, immediate recall exclusion, idempotent delete operation, and bounded audit event
- [x] 7.6 Complete hard deletion after provider success by deleting current/revision content and writing a content-free tombstone
- [x] 7.7 Keep failed provider deletion fail-closed and retryable, retain only required external mapping, and expose dead-letter state without reactivating memory
- [x] 7.8 Extend TTL recall filtering and implement TTL sweeper using the same hard-delete lifecycle operation
- [x] 7.9 Ensure user/TTL deletion removes directly associated pending capsule/job content and invalidates frontend/debug caches where applicable
- [x] 7.10 Implement tenant-partitioned consolidation for exact/semantic duplicates, session summaries, stale task projections, and conflict pending through normal policy/revision paths
- [x] 7.11 Add lifecycle tests for ambiguous/repeated delete, mem0 failure, TTL race, tombstone redaction, consolidation revision, and cross-tenant prohibition
  - 2026-07-13 group review: a new independent lifecycle reviewer approved the complete group after three review passes and fixes for cleanup bypasses, immutable replay identity, SQLite/PostgreSQL delete races, completed-provider hard-delete gating, dead-letter minimization, tenant-partitioned consolidation discovery, summary-to-fact privilege escalation, and tombstone target binding. Final verification: 115 focused and 390 full backend tests, Ruff lint/format, strict OpenSpec validation, diff checks, SQLite 20-run sweeper races, and temporary-schema PostgreSQL forced-overlap probes passed. Only the existing Starlette deprecation warning remains.

## 8. mem0 Adapter And Index Consistency

- [x] 8.1 Add fake mem0 tests proving every governed ADD passes one canonical content item with `infer=False` and never passes the full Turn Capsule message list
- [x] 8.2 Extend `MemoryStrategyAdapter` and `Mem0MemoryAdapter` with update and index-operation result contracts while preserving repository fallback compatibility
- [x] 8.3 Change explicit and automatic accepted ADD paths to pass `infer=False`, complete metadata, and stable OIR memory/revision IDs
- [x] 8.4 Implement mem0 update by known external ID and metadata refresh without creating a second logical OIR memory
- [x] 8.5 Strengthen delete handling for not-found idempotency, retryable provider errors, external ID mapping, and deletion status events
- [x] 8.6 Validate mem0 search results against canonical active/lifecycle/TTL/tenant/subject state before building `memory_context`
- [x] 8.7 Implement add-crash recovery/adoption by OIR memory ID metadata and detect duplicate/orphan provider records
- [x] 8.8 Implement index repair/reindex service for missing mappings, out-of-sync revisions, duplicate/orphan vectors, and full rebuild from active PostgreSQL projections
- [x] 8.9 Add mem0 degraded/fail-closed tests for add/update/delete/search/repair and verify provider success is never reported on fallback/error
- [x] 8.10 Extend the real PostgreSQL + mem0 2.0.11 + Milvus Lite smoke helper to verify infer-false single vector, update-in-place, hard delete, and rebuild
  - 2026-07-13 group review: the independent mem0/index-consistency reviewer approved the complete group after four review passes and fixes for trusted metadata precedence, immutable repair pagination, canonical CAS revalidation, Agent/source isolation, durable provider outcomes, and durable CAS-loss compensation deletes. Final verification: 73 focused and 416 full backend tests, Ruff lint/format, strict OpenSpec validation, and diff checks passed. Memory and SQLite process-restart probes proved compensation retry idempotency, no duplicate canonical hard-delete, retained completion metadata, and eventual provider-vector removal. The real PostgreSQL + mem0ai 2.0.11 + Milvus Lite smoke passed infer-false single-vector ADD, stable external-ID UPDATE, provider-gated hard delete, canonical rebuild, and lifecycle cleanup. Only the existing Starlette deprecation warning remains.

## 9. Invocation, Event, Plan, And Context Integration

- [x] 9.1 Inject turn-capture/trigger dependencies into invocation services without coupling invokers to memory implementation details
- [x] 9.2 Hook Turn Capsule persistence after AgentRun/AgentResult persistence for direct invoke and route-and-invoke, preserving main response latency and status
- [x] 9.3 Publish structured formation commands after Plan save/confirm/cancel and Agent event-driven Plan transitions
- [x] 9.4 Publish structured formation commands after Run/Result/Artifact state is durably persisted and make duplicate events idempotent
- [x] 9.5 Project active task memory with derived authority and canonical plan/current-step refs into the existing governed route/agent context provider path
- [x] 9.6 Ensure current input and canonical active Plan override stale task memory, and Agent execution reloads Plan by ID before continuing
- [x] 9.7 Add integration tests for “继续上次任务” resolving active Plan without a new intent category and unrelated new tasks ignoring old task memory
- [x] 9.8 Add integration tests proving route-only/incomplete turns, failed invocations, duplicate events, and feature-off modes do not produce unintended long-term memory
- [x] 9.9 Bind and overwrite required Plan user/tenant ownership from trusted UserContext before final route Plan validation and persistence, including direct Plan creation/execution paths
- [x] 9.10 Enforce stored Plan ownership for get, active lookup, confirm, cancel, execute, Agent-event update, task projection, and continuation context
  - 2026-07-13 group review: the same independent invocation/plan/context reviewer approved the complete group after repeated adversarial passes and fixes for owner-scoped events/results/history, Plan state-version CAS and claim fencing, blocked/progress ordering, heartbeat plus stable downstream idempotency keys, durable Plan/Run/Result/Turn reconciliation, private/off atomic suppression and skipped-trace retry, exact structured-command replay, route/Agent memory scope and cache isolation, canonical Result/Run ownership, and lossless business JSON/Capsule restart recovery. Final verification: 106 focused and 471 full backend tests in the main review, 100 additional independent focused tests, 9 frontend tests, Ruff lint/format, strict OpenSpec validation, diff checks, SQLite restart/race probes, and real PostgreSQL probes for claim 1→2→3, single-job Plan replay, suppression, reconciliation, and Run/Result round-trip all passed. External Agent side effects remain explicitly at-least-once and require transactional handling of the stable idempotency key. Only the existing Starlette deprecation warning remains.

## 10. Debug, Management API, And Metrics

- [x] 10.1 Extend memory debug response schemas and endpoints with authorized request/session/turn/run/job/memory-key/decision filters
- [x] 10.2 Add bounded Formation Trace assembly for trigger, source range, versions, attempts, decisions, revisions, provider/index status, latency, and usage
- [x] 10.3 Keep Context Trace and Formation Trace separately modeled and linked by stable refs without adding debug payloads to Router/Agent model inputs
- [x] 10.4 Add user-owned memory lifecycle endpoints for delete, pending confirm/reject, operation status, and idempotency/precondition handling
- [x] 10.5 Add admin cross-subject management authorization, actor/reason audit, and non-disclosing authorization failures
- [x] 10.6 Redact candidate quotes, formation prompts, secrets, regulated values, provider credentials, tombstones, and errors in events/API/logs
- [x] 10.7 Add runtime/admin views for worker state, queue depth, oldest pending age, dead-letter, index out-of-sync, and last safe error
- [x] 10.8 Add metrics for job/model latency and usage, decision rates, retries/dead-letter, pending age, index repair, deletion completion, and recall use without content labels
- [x] 10.9 Add API/auth/redaction tests for every filter and lifecycle management operation, including cross-tenant denial
  - 2026-07-14 group review: the same independent Debug/Management/Metrics reviewer approved the complete group after adversarial passes and fixes for trusted user/admin identity, claim/crash recovery, effective pending resolution before query limits, hard-delete replay, bounded SQL trace filters, indexed terminal-decision linkage and legacy backfill, mode-off maintenance/recall linking, recursive redaction, allowlisted usage keys, full-database rollout metrics, and explicit local tenant requirements. Final verification: 499 full backend tests, 9 frontend tests, Ruff lint/format, strict OpenSpec validation, diff checks, a 10,001-row aggregate regression, and real PostgreSQL old-row decision backfill/full-metrics probes passed with no residual schemas. Only the existing Starlette deprecation warning remains.

## 11. Conversation Console

- [x] 11.1 Extend frontend types and per-turn state for actual Recall Used, formation job refs, asynchronous decisions, lifecycle/index status, and pending operations
- [x] 11.2 Add compact assistant-turn badges for Recall Used and formation ADD/UPDATE/DELETE/NOOP/REJECT/PENDING counts without placing technical details in chat text
- [x] 11.3 Split the selected-turn Memory inspector into Recall Used and Formation / Write Decisions with stable loading/empty/pending/error states
- [x] 11.4 Fetch or subscribe to turn/job formation status after the original response and associate one multi-turn job without duplicating decisions
- [x] 11.5 Ensure selecting an older turn uses only its trace and never derives recall/write counts from global Memory Debug state
- [x] 11.6 Add bounded decision details and redact sensitive rejected content in previews and raw JSON views
- [x] 11.7 Add authorized confirm/reject/delete controls with destructive confirmation, idempotency/precondition payload, and pending/completed/conflict feedback
- [x] 11.8 Preserve the global Memory Debug management view as a distinct repository-level surface and show formation filters/status without conflating it with selected-turn trace
- [x] 11.9 Add frontend unit/E2E tests for delayed idle updates, five-turn ranges, older-turn selection, pending controls, denied operations, errors, and redaction
  - 2026-07-14 group review: the same independent Conversation Console reviewer approved the complete group after fixes for bounded Recall relevance/confidence, stale polling response protection, final-operation PENDING counts, and source/request/run/canonical refs. Final verification: frontend typecheck, 18 unit/integration tests including real two-second polling, out-of-order responses and conflict feedback, and production build passed; 65 implementation-focused and 72 independent-review backend tests plus 499 full backend tests passed; Ruff lint/format, strict OpenSpec validation, and `git diff --check` passed. In-app browser QA at desktop and 390x844 mobile widths found no horizontal overflow, overlap, or console errors. Only the existing Starlette deprecation warning remains.

## 12. End-To-End Verification And Rollout

- [x] 12.1 Add backend unit tests for all formation/job/policy/lifecycle reason codes and target at least one scenario per OpenSpec requirement
- [x] 12.2 Add database integration tests for migrations, restart recovery, multi-worker races, revision consistency, deletion pending, and outbox repair
- [x] 12.3 Add service integration tests covering five turns, thirty-second idle, no session close, structured immediate projection, main-response independence, and rollout modes
- [x] 12.4 Add end-to-end isolation tests proving tenant/user/subject separation across formation, dedupe, consolidation, debug, management, and recall
- [x] 12.5 Add privacy tests proving temporary mode skips buffering and user/TTL deletion removes current/revision/provider data plus directly associated pending payload content
- [x] 12.6 Run the full backend and frontend regression suites and resolve compatibility regressions in explicit write-candidates, existing memory_context, context pipeline, and debug console
- [x] 12.7 Run and document real infrastructure smoke for preference ADD/recall/UPDATE/delete and unfinished Plan continuation versus unrelated new task
- [x] 12.8 Update `.env.example`, runtime/config docs, mem0 integration docs, Debug/E2E test plan, and the strategy document with final implemented contracts without exposing credentials
- [x] 12.9 Deploy schema/services with mode off, validate backfill, run observe-mode quality review, then enable enforced for an isolated tenant
- [x] 12.10 Define rollout gates for queue/dead-letter/index/delete health, formation precision/correction/NOOP rates, cost/latency, and an emergency mode-off rollback drill
- [x] 12.11 Add Plan ownership E2E tests for missing tenant/user rejection, forged ownership overwrite, same-tenant cross-user denial, event ownership preservation, and owned continuation
  - 2026-07-14 implementation verification: the requirement/reason-code matrix, cross-tenant lifecycle E2E, database recovery/race suites, rollout-mode/temporary/delete tests, and Plan ownership tests passed in a 179-test focused run. The expanded real PostgreSQL + mem0ai 2.0.11 + Milvus Lite smoke passed preference ADD/recall/revision UPDATE/delete, stable external-ID single-vector indexing, canonical rebuild, owned Plan continuation, and unrelated-task isolation. Final pre-review gates passed with 504 backend tests, frontend typecheck plus 18 tests and production build, Ruff lint/format, strict OpenSpec validation, and `git diff --check`. The existing Starlette deprecation warning and non-blocking PostHog/gRPC/optional-spaCy smoke messages remain.
  - 2026-07-14 review remediation: after the first independent review rejected the group, trusted signed identity was enforced for route/invoke/Plan HTTP paths; the acceptance matrix now verifies pytest collection and `temporary_request` is emitted at runtime; a real PostgreSQL test covers advisory-lock formation convergence, revision `FOR UPDATE` CAS, and outbox `SKIP LOCKED`; the E2E covers same-owner cross-subject formation/recall/consolidation/debug/management isolation; TTL and real smoke verify revision/provider/pending-payload hard deletion; smoke claims only its tenant and asserts failure-safe provider/Plan/job/ledger cleanup; rollout evidence now records sample denominators and executable health/metric formulas without claiming production approval. Post-fix verification: 67 remediation-focused and 508 full backend tests, 18 frontend tests plus typecheck/build, Ruff lint/format, strict OpenSpec validation, diff checks, and the hardened real infrastructure smoke passed.
  - 2026-07-14 independent group review: the reviewer rejected the first pass with seven findings, then approved the complete group after two remediation reviews. The final pass confirmed signed route/invoke/Plan ownership, executable requirement/reason-code coverage, real PostgreSQL concurrency, subject isolation, full TTL/provider/revision deletion, tenant-scoped failure-safe smoke cleanup, and auditable rollout limits/gates. A final wiring fix made explicit write-candidates reuse the same tenant-scoped smoke worker/outbox/store; 45 related tests and the real infrastructure smoke passed again. `2.9` remains intentionally unchecked, and this review does not authorize archiving the change.

## 13. Structured Semantic Contracts And Formation Model

- [x] 13.1 Add strict enums/schemas for semantic target, slot, strict JSON value, temporal scope, polarity, certainty, and change/delete intent without breaking canonical structured-event identity fields
- [x] 13.2 Require ordinary `ConversationFormationResponse` candidates to contain complete structured semantics and make missing/invalid fields a retryable invalid model response with no partial side effects
- [x] 13.3 Update the bounded formation prompt so the model distinguishes user response preferences from quoted/template/document content and emits unknown/uncertain instead of guessing
- [x] 13.4 Update fake/OpenAI-compatible formation model tests for multilingual equivalents, negation, current-turn overrides, quoted/meta text, multi-intent turns, schema failures, and bounded output
- [x] 13.5 Make deterministic StructuredEventProjector candidates populate the same semantic contract from canonical fields without invoking natural-language inference
- [x] 13.6 Complete an independent review of the whole Structured Semantic Contracts And Formation Model group before starting group 14
  - 2026-07-14 group review: the independent reviewer approved the complete group after rejecting the first pass and verifying fixes for injected-model schema bypass and strict JSON semantic values. The final review confirmed required seven-field semantics at the processor boundary, retryable no-side-effect failure for invalid model output, bounded prompt/schema behavior, canonical StructuredEventProjector semantics, and the side-effect-free verifier contract. Final verification: 106 independent focused and 592 full backend tests, Ruff lint/format, strict OpenSpec validation, and `git diff --check` passed. Only the existing Starlette deprecation warning remains.

## 14. Policy Layer Split And Uncertainty

- [x] 14.1 Extract `MemoryCandidateHardRules` for trusted identity/tenant/subject, scope, frozen evidence refs, DLP, target ownership/uniqueness, TTL/lifecycle reasons, key/hash/idempotency, dedupe, delete authorization, and redacted audit projection
- [x] 14.2 Implement `MemoryCandidateSemanticValidator` that validates only structured target/slot/value/temporal_scope/polarity/certainty/intent consistency against operation, scope, evidence role, and current projection
- [x] 14.3 Reduce `MemoryCandidatePolicy` to orchestration/current-state thresholds and move hard/semantic helpers out of the monolithic module while preserving the public policy/result contract
- [x] 14.4 Remove natural-language regex/token-overlap authorization from automatic ADD/UPDATE/DELETE; any temporary safety filter must be isolated and may only downgrade to PENDING/REJECT
- [x] 14.5 Wire the side-effect-free `MemorySemanticVerifier` protocol and bounded verdict schema; verifier absent/error/invalid/uncertain keeps PENDING and confirmed verdict still re-runs hard rules/preconditions
- [x] 14.6 Add focused tests for every hard-rule branch, structured semantic consistency branch, verifier outcome, current revision race, and proof that model/verifier cannot access lifecycle/provider side effects
- [x] 14.7 Preserve explicit write-candidates and canonical structured lifecycle behavior through an explicit structured compatibility adapter rather than evidence-language parsing
- [x] 14.8 Complete an independent review of the whole Policy Layer Split And Uncertainty group before starting group 15
  - 2026-07-14 group review: the independent reviewer rejected the first pass because ordinary Assistant-only task/artifact/session candidates could pass hard rules and their semantic values were not checked against structured projections. The remediation requires frozen user or trusted canonical evidence for every durable scope and validates task status, artifact identity/URI, and session summary values across semantic and structured fields. The reviewer reran the original exploit and confirmed reject/pending outcomes, then approved the complete group with no remaining findings. Final verification: 263 policy/lifecycle/explicit focused tests, 40 structured/E2E/indexing review tests, 617 full backend tests, Ruff lint/format, strict OpenSpec validation, and `git diff --check` passed. Only the existing Starlette and intermittent aiosqlite shutdown warnings remain.

## 15. Integration, Observability, And Regression

- [x] 15.1 Wire processor/dependencies so conversation model semantics, canonical projector semantics, hard rules, semantic validator, optional verifier, policy, and lifecycle remain separate injectable stages
- [x] 15.2 Extend bounded Formation Trace/debug metadata with semantic validation/verifier outcome and versions without exposing prompts, full quotes, or sensitive structured values
- [x] 15.3 Replace policy-regex language tests with formation-model contract fixtures plus structured policy tests, retaining only explicit temporary safety-filter tests
- [x] 15.4 Add end-to-end tests proving multilingual response preference ADD/UPDATE, quoted/template content PENDING/REJECT, current-turn NOOP, ambiguous semantics PENDING, delete authorization, tenant isolation, and no verifier side effects
- [x] 15.5 Run full backend/frontend regressions, strict OpenSpec validation, real frontend idle-formation display, and update strategy/debug/rollout docs for the final three-layer boundary
  - 2026-07-14 implementation verification: the dependency graph keeps conversation-model semantics, canonical structured projection, deterministic hard rules, structured semantic validation, temporary downgrade-only safety filtering, optional read-only verifier, policy orchestration, and lifecycle side effects as separate injectable boundaries. Formation Trace/debug metadata exposes only bounded semantic contract/validation/verifier versions and outcome counts. Formation-model fixtures, structured policy tests, verifier race/no-side-effect tests, and multilingual/quoted/current-turn/delete/tenant-isolation E2E coverage passed. Final gates passed with 622 backend tests, 18 frontend tests, frontend production build, Ruff lint/format, strict OpenSpec validation, and `git diff --check`; only the existing Starlette deprecation and intermittent aiosqlite shutdown warnings remain. A real in-app-browser idle run waited over 30 seconds and showed `ADD 1`, accepted `user_preference`, index/provider success, the matching global Memory Item, revision, event, and Formation Trace for job `mfjob_eba44d807658455d9c404d75592c48bc`; screenshots are stored under `artifacts/ui-memory-formation/06-three-layer-turn-formation.png` through `09-three-layer-semantic-trace.png`, and a clean full reload plus interaction produced no application console errors.
  - 2026-07-14 review remediation: the first independent review found four blockers. The remediation now projects only allowlisted semantic contract/validation/verifier counts through internal trace schema, repository mapping, public Debug view and frontend; strips pending `semantic.value` and `structured_value` only from the public event projection while retaining the internal resolution snapshot; records `current_turn` and actual safety-filter semantic outcomes instead of defaulting to confirmed; and adds processor-level acceptance tests that traverse model, policy, lifecycle and verifier for Chinese ADD, Spanish UPDATE, quoted-template PENDING, current-turn NOOP, ambiguous PENDING, authorized/cross-tenant DELETE, verifier uncertainty, no lifecycle/index side effect and current-state race revalidation. The original leak marker is absent from serialized Debug output and the real frontend now displays `semantic v1`, `validation confirmed 1`, and `verifier -` for the demo job.
- [x] 15.6 Complete an independent review of the whole Integration, Observability, And Regression group and resolve all findings before marking the change all done
  - 2026-07-14 independent group review: the reviewer rejected the first pass with four blockers: public Debug leaked pending `semantic.value`; semantic version/validation/verifier counts stopped at the raw job summary instead of reaching trace/API/UI; current-turn and temporary safety-filter outcomes could be mislabeled confirmed; and the claimed 15.4 scenarios were not covered through the complete model-to-lifecycle pipeline. The second pass approved the group after executable marker reproduction proved internal pending resolution remains intact while public Debug removes marker/structured/semantic values, allowlisted semantic metadata reached in-memory/database trace repositories and frontend UI, outcome counts became accurate, and new Fake model -> processor -> policy -> lifecycle/verifier acceptance tests covered the full multilingual/quoted/current-turn/ambiguous/delete/isolation/verifier matrix. Independent verification passed with 38 focused backend tests and 18 frontend tests; no new explicit-write, structured-projection, pending-resolution, or tenant-isolation regression was found. The full implementation gates passed with 622 backend tests, frontend test/build, Ruff lint/format, strict OpenSpec validation, and `git diff --check`. This approval completes the change implementation but does not archive it.
