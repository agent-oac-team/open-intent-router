# Acceptance Evidence

## Real request-to-recall closure

- Input: `明天要拜访一位关注稳健理财的客户，帮我做访前准备。我喜欢吃猪肉。`
- Request ID: `req_memory_fix_e2e_20260717_01`
- Canonical Turn: `completed`
- Run/Result: `completed`
- Turn Outbox: `completed`
- Formation Job: `completed`
- Memory content: `User likes eating pork.`
- Revision ID: `mrev_87176829d5573cc1873895d0d0292ffc`
- Index status: `ready`
- Recall query: `我喜欢吃什么？`
- Recall result: `User likes eating pork.`
- Debug stage: `request_trace.overall_stage=persisted`

## Automated closure

- `tests/test_router_turn_integration.py` runs the same multi-intent sentence through Router,
  Canonical invocation, a restarted Outbox consumer, deterministic Formation, a restarted index
  worker, and subsequent recall.
- `tests/test_memory_postgresql_integration.py::test_real_postgresql_route_turn_to_recall_pork_preference`
  runs Canonical transactions, Formation, Revision, index, recall, and Debug `persisted` against the
  configured real PostgreSQL database with an isolated tenant/user.
- Frontend tests keep pending/retry/dead-letter/index-pending/trace-missing/persisted states visible
  and retain manual refresh after bounded polling.

## Existing-data reconciliation

The existing database was scanned in dry-run mode only. No historical row was modified.

- Scope: `tenant_a/u1`, updated before `2026-07-17T10:56:57Z`
- `repairable_enabled`: 1
- `repairable_skipped`: 1
- `ambiguous`: 0
- `ownership_conflict`: 0
- Report: `evidence/orphan-dry-run.json`

The suppressed record remains excluded from automatic backfill. Any repair requires an explicit
request ID, `--apply`, and an operator-provided idempotency key.
