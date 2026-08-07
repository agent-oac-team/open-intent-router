## 1. Native Principal contract

- [x] 1.1 Add focused security tests for a fully signed `oir-principal-v1` Envelope, invalid or missing non-local signatures, unsigned local-loopback access, legacy owner-only signatures, discarded body privilege claims, and rejected subject or tenant conflicts.
- [x] 1.2 Implement the host-neutral `NativePrincipal` schema, canonical Envelope encoding and HMAC verification, configuration, and `require_native_principal` dependency in Core; use constant-time signature comparison and map the legacy identity contract to empty privilege sets.
- [x] 1.3 Derive the existing Memory actor and Route, Invoke, Plan, Run, Session, Event, and personalized Available-Agent request identities from `NativePrincipal`, then update shared test/client helpers and the local test UI without adding a production compatibility bypass.

## 2. Canonical Ownership and Agent selection

- [x] 2.1 Add negative integration tests proving cross-subject and cross-tenant Run, Plan, and Session access returns `404`, owner fields cannot redirect reads or writes, admin-like roles do not bypass owner endpoints, and the public sanitized Agent catalog remains public.
- [x] 2.2 Add explicit owner-scoped Run and Session query/write paths and bind Native Run, Plan, Session, and `/agents/available` APIs to the Principal's `(tenant_id, subject)`; retain unscoped maintenance access only behind internal service boundaries.
- [x] 2.3 Add regression tests proving Direct Invoke filters before Context, Run, Result, or Invoker side effects; route-and-invoke evaluates availability once; and each later Plan confirm, execute, confirm-and-execute, or resume request creates a fresh Candidate Set exactly once.
- [x] 2.4 Expose one Registry operation that returns available `AgentDefinition` objects, make Direct Invoke consume its selected definition, and make route-and-invoke reuse the Candidate Set already produced by routing instead of evaluating access policy again.
- [x] 2.5 Make Plan execution preflight every non-terminal Step against the request's one Candidate Set before any invocation; return `404 agent_not_available` and preserve the Plan when any target is missing, disabled, or unauthorized.

## 3. Execution Ticket authority for Native Events

- [x] 3.1 Add Native Event API tests for a matching Ticket, missing or invalid signatures, expiry and purpose mismatch, run/turn/owner/agent/plan/step conflicts, duplicate event IDs, and absence of partial Event, Result, Plan, Turn, or Outbox writes on rejection.
- [x] 3.2 Move reusable Execution Ticket configuration and dependency assembly into Core while keeping OAC `OIR-HOST-V2`, Legacy body projection, and frozen fixtures unchanged; do not add a Native Ticket-issuance endpoint.
- [x] 3.3 Change the Native Event API to accept `X-OIR-Execution-Ticket`, derive execution identity from verified claims, release a claimed Ticket after progress, consume it after a committed terminal event, and preserve trusted in-process service calls for internal Invokers.

## 4. Deterministic Plan DAG execution

- [x] 4.1 Add regression tests for an unordered acyclic Plan, a non-ready `current_step_id` with another ready Step, multiple ready Steps in original-list order, dependency blocking, invalid graphs, fail-fast behavior, and incomplete unexplained no-ready state.
- [x] 4.2 Initialize `current_step_id` from the first ready Step and refactor PlanExecutor to recompute the full ready-set after each result, execute serially with stable original-list tie-breaking, and never invoke a Step before all dependencies complete.
- [x] 4.3 Enforce strict Plan terminal rules: complete only when every Step completed, fail immediately on a non-recoverable Step failure while leaving never-started Steps pending, preserve declared blocked states, and raise an invariant conflict rather than fabricating completion.

## 5. Truthful Delegated Run cancellation

- [x] 5.1 Extend Memory and Database contract tests for cancellation confirmation, including complete Ticket correlation, expected state version, atomic Run/Turn/Plan-Step/Event/Outbox convergence, identical-event idempotency, and terminal-state conflicts; assert the real `DelegatedRunService` satisfies `DelegatedRunApplicationPort`.
- [x] 5.2 Implement Memory and Database cancel store operations and `DelegatedRunService.cancel`, wire the service through the application port, and keep the transition atomic without introducing a dispatcher, control queue, or unreachable `cancel_pending` state.
- [x] 5.3 Add Plan cancellation API tests for unstarted work, an active Run with no control channel, repeat requests, owner isolation, and the exact HTTP 200 `accepted=false`, `transitioned=false`, `reason_code=control_unsupported` response with unchanged state version.
- [x] 5.4 Detect active Delegated Runs during Plan cancellation, cancel only unstarted work when none exists, and otherwise return the structured `control_unsupported` domain result without changing or dispatching execution state.

## 6. Deadline convergence runtime

- [x] 6.1 Add Memory and Database tests for selecting only non-terminal Runs at or past `deadline_at`, deterministic timeout event IDs, stale heartbeat before deadline, restart idempotency, terminal-event races, batching, and clean application shutdown.
- [x] 6.2 Add explicit overdue-run store queries and implement `DelegatedRunTimeoutRuntime` to call the existing idempotent timeout command, skip version conflicts for a later canonical re-read, and avoid heartbeat-based timeout or retry behavior.
- [x] 6.3 Add interval and batch configuration, start the timeout runtime in the existing FastAPI lifespan for Native and OAC composition, and await its background task on shutdown.

## 7. Documentation and release gates

- [x] 7.1 Update Native API/security documentation, configuration examples, local test UI guidance, Candidate Set and ownership semantics, Ticket callback requirements, cancellation response, and deadline behavior; explicitly document the unchanged public catalog and OAC Legacy contract.
- [x] 7.2 Run the focused security, routing/invocation, ownership, Plan DAG, Execution Ticket, Delegated Run cancel/timeout, OAC frozen-contract, and documentation-harness test suites and resolve all regressions.
- [x] 7.3 Run the full pytest suite, Ruff check and format verification, `git diff --check`, and `openspec validate harden-native-execution-p0 --strict`; leave no new dependency, database migration, or out-of-scope execution abstraction.

## 8. Review remediation

- [x] 8.1 Add Native Event HTTP red tests proving a consumed Ticket accepts only an identical terminal replay, while a same-ID replay with changed event type, status, or canonical payload returns a conflict without changing canonical or Ticket state; implement the minimum identity validation needed to pass them.
- [x] 8.2 Add deterministic same-ID concurrent terminal callback tests; prevent an active same-owner Ticket claim from replacing its lease token, and make a committed duplicate terminal callback converge the Ticket to `consumed` rather than release it.
- [x] 8.3 Add server-clock tests proving client `created_at` cannot expire, extend, or backdate Ticket lease bookkeeping; separate canonical event occurrence time from Ticket claim, release, and consume time.
- [x] 8.4 Move Native Event claim, correlation, command, and finalization orchestration from `app/api` into one Core application/service boundary; keep the HTTP layer adapter-only and preserve the trusted in-process event path without adding a generic runtime abstraction.
- [x] 8.5 Add Memory and Database cancellation/start race tests and make “confirm no active Delegated Run, then cancel unstarted Plan work” atomic with Delegated Run start, so no committed outcome can contain both a cancelled Plan and a newly active Run.
- [x] 8.6 Run the focused remediation suites, full release gates, strict OpenSpec validation, and repeat the Standards/Spec code review against the current `HEAD`.

## 9. Review remediation round 2

- [x] 9.1 Add a Native progress-event crash-window red test and make an identical committed replay advance stale Ticket run-version/event-sequence cursors before releasing its claim.
- [x] 9.2 Add request-scoped Candidate Set mutation tests and carry the selected `AgentDefinition` objects from Route through Route-and-Invoke and Route-and-Execute without re-reading mutable Registry definitions.
- [x] 9.3 Add terminal replay metadata red tests and require explicit sequence and occurrence-time identity while reusing the persisted occurrence time when a retry omits `created_at`.
- [x] 9.4 Add Memory and Database delegated-completion DAG red tests and derive the next Plan state from dependency-ready Steps, completing only when every Step is completed.
- [x] 9.5 Add Memory and Database delegated-clarification red tests and atomically project `agent_clarify` to the associated blocked Plan Step while preserving any applicable `next_action`.
- [x] 9.6 Run the focused remediation suites, full release gates, strict OpenSpec validation, and repeat the Standards/Spec code review against the current `HEAD`.

## 10. Review remediation round 3

- [x] 10.1 Add a deterministic Database Ticket concurrency red test and make claim acquisition atomic across service processes without relying on a process-local lock.
- [x] 10.2 Add an OAC Plan-confirm red test proving a currently unavailable Agent prevents confirmation without changing Plan state, and form the request-scoped Candidate Set before the confirm transition.
- [x] 10.3 Run the focused remediation suites, full release gates, strict OpenSpec validation, and repeat the Standards/Spec code review against the current `HEAD`.

## 11. Review remediation round 4

- [x] 11.1 Add deterministic Database Ticket stale-worker red tests for release, consume, and progress finalization, then make each claimed transition a Store-level CAS bound to the current lease.
- [x] 11.2 Run the focused remediation suites, full release gates, strict OpenSpec validation, and repeat the Standards/Spec code review against the current `HEAD`.

## 12. Review remediation round 5

- [x] 12.1 Add a deterministic real-PostgreSQL cancel-versus-terminal lock-order red test and make Plan cancellation acquire Run before Plan so competing transactions cannot deadlock.
- [x] 12.2 Add Memory and Database red tests proving a colliding progress Event cannot preempt timeout convergence, then validate the full canonical timeout Event identity before treating it as a duplicate.
- [x] 12.3 Run the focused remediation suites, full release gates, strict OpenSpec validation, and repeat the Standards/Spec code review against the current `HEAD`.

## 13. Review remediation round 6

- [x] 13.1 Add a deterministic real-PostgreSQL start-gap red test and make the Plan-locked active-Run recheck non-locking, preserving Run-before-Plan order while observing a just-committed start; require an explicit test URL and cleanup on every path.
- [x] 13.2 Run the focused remediation suites, full release gates, strict OpenSpec validation, and repeat the Standards/Spec code review against the current `HEAD`.
