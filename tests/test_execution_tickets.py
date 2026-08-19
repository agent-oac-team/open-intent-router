import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.database import DatabaseRunRepository
from app.repositories.execution_tickets import (
    DatabaseExecutionTicketStore,
    ExecutionTicketConflict,
    MemoryExecutionTicketStore,
    ticket_hash,
)
from app.schemas.delegated_runs import DelegatedRunReference, DelegatedRunStatus
from app.schemas.execution_tickets import (
    ExecutionTicketStatus,
    LegacyExecutionCorrelationQuery,
)
from app.schemas.logs import AgentRun
from app.services.execution_ticket_service import ExecutionTicketError, ExecutionTicketService


@pytest.fixture(params=["memory", "database"])
async def tickets(request, tmp_path):
    if request.param == "memory":
        store = MemoryExecutionTicketStore()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'tickets.db'}",
        )
        await create_all_tables(settings)
        store = DatabaseExecutionTicketStore(create_session_factory(settings))
    return ExecutionTicketService(store, secret="ticket-secret")


def _run() -> DelegatedRunReference:
    return DelegatedRunReference(
        run_id="run-1",
        turn_id="turn-1",
        tenant_id="tenant-1",
        user_id="user-1",
        agent_id="agent-1",
        plan_id="plan-1",
        step_id="step-1",
        status=DelegatedRunStatus.RUNNING,
        state_version=1,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def test_issue_stores_only_hash_and_validates_owner_purpose_and_expiry(tickets) -> None:
    now = datetime.now(UTC)
    issued = await tickets.issue(
        _run(), request_id="request-1", purpose="agent_event", ttl_seconds=60, now=now
    )
    stored = await tickets.store.get(ticket_hash(issued.ticket))

    assert stored is not None
    assert issued.ticket not in stored.model_dump_json()
    assert stored.claims.nonce
    assert await tickets.resolve(
        issued.ticket,
        tenant_id="tenant-1",
        user_id="user-1",
        purpose="agent_event",
        now=now,
    )

    for values in [
        {"ticket": issued.ticket + "x", "tenant_id": "tenant-1", "user_id": "user-1"},
        {"ticket": issued.ticket, "tenant_id": "tenant-1", "user_id": "other-user"},
    ]:
        with pytest.raises(ExecutionTicketError, match="execution ticket"):
            await tickets.resolve(
                purpose="agent_event",
                now=now,
                **values,
            )
    with pytest.raises(ExecutionTicketError, match="expired"):
        await tickets.resolve(
            issued.ticket,
            tenant_id="tenant-1",
            user_id="user-1",
            purpose="agent_event",
            now=now + timedelta(seconds=61),
        )


async def test_claim_recovery_and_consume_are_idempotent(tickets) -> None:
    now = datetime.now(UTC)
    issued = await tickets.issue(
        _run(), request_id="request-1", purpose="agent_event", ttl_seconds=120, now=now
    )
    first = await tickets.claim(
        issued.ticket,
        tenant_id="tenant-1",
        user_id="user-1",
        purpose="agent_event",
        owner="worker-a",
        lease_seconds=10,
        now=now,
    )
    with pytest.raises(ExecutionTicketError, match="claimed"):
        await tickets.claim(
            issued.ticket,
            tenant_id="tenant-1",
            user_id="user-1",
            purpose="agent_event",
            owner="worker-b",
            lease_seconds=10,
            now=now + timedelta(seconds=1),
        )
    recovered = await tickets.claim(
        issued.ticket,
        tenant_id="tenant-1",
        user_id="user-1",
        purpose="agent_event",
        owner="worker-b",
        lease_seconds=10,
        now=now + timedelta(seconds=11),
    )
    assert recovered.record.lease_token != first.record.lease_token

    consumed = await tickets.consume(
        issued.ticket,
        event_id="event-1",
        owner="worker-b",
        lease_token=recovered.record.lease_token or "",
        now=now + timedelta(seconds=12),
    )
    duplicate = await tickets.consume(
        issued.ticket,
        event_id="event-1",
        owner="worker-b",
        lease_token="ignored-after-consume",
        now=now + timedelta(seconds=13),
    )
    assert consumed.status == ExecutionTicketStatus.CONSUMED
    assert duplicate.consumed_event_id == "event-1"
    with pytest.raises(ExecutionTicketError, match="already consumed"):
        await tickets.consume(
            issued.ticket,
            event_id="event-2",
            owner="worker-b",
            lease_token="ignored",
            now=now + timedelta(seconds=14),
        )


async def test_same_owner_cannot_replace_the_active_lease(tickets) -> None:
    now = datetime.now(UTC)
    issued = await tickets.issue(
        _run(), request_id="request-1", purpose="agent_event", ttl_seconds=120, now=now
    )

    first = await tickets.claim(
        issued.ticket,
        tenant_id="tenant-1",
        user_id="user-1",
        purpose="agent_event",
        owner="native-agent-event:event-1",
        lease_seconds=10,
        now=now,
    )
    with pytest.raises(ExecutionTicketError, match="claimed"):
        await tickets.claim(
            issued.ticket,
            tenant_id="tenant-1",
            user_id="user-1",
            purpose="agent_event",
            owner="native-agent-event:event-1",
            lease_seconds=10,
            now=now + timedelta(seconds=1),
        )

    stored = await tickets.store.get(ticket_hash(issued.ticket))
    assert stored is not None
    assert stored.lease_token == first.record.lease_token
    assert stored.lease_expires_at == first.record.lease_expires_at


async def test_database_claim_is_atomic_across_service_instances(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'concurrent-tickets.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    issuer = ExecutionTicketService(
        DatabaseExecutionTicketStore(session_factory), secret="ticket-secret"
    )
    now = datetime.now(UTC)
    issued = await issuer.issue(
        _run(), request_id="request-1", purpose="agent_event", ttl_seconds=120, now=now
    )

    reads_ready = asyncio.Event()
    reads = 0

    def coordinated_store() -> DatabaseExecutionTicketStore:
        store = DatabaseExecutionTicketStore(session_factory)
        original_get = store.get

        async def coordinated_get(ticket_hash_value: str):
            nonlocal reads
            record = await original_get(ticket_hash_value)
            reads += 1
            if reads == 2:
                reads_ready.set()
            elif reads == 1:
                await reads_ready.wait()
            return record

        store.get = coordinated_get  # type: ignore[method-assign]
        return store

    services = [
        ExecutionTicketService(coordinated_store(), secret="ticket-secret"),
        ExecutionTicketService(coordinated_store(), secret="ticket-secret"),
    ]

    outcomes = await asyncio.gather(
        *(
            service.claim(
                issued.ticket,
                tenant_id="tenant-1",
                user_id="user-1",
                purpose="agent_event",
                owner=f"worker-{index}",
                lease_seconds=30,
                now=now,
            )
            for index, service in enumerate(services)
        ),
        return_exceptions=True,
    )

    claims = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, ExecutionTicketError)]
    assert len(claims) == 1
    assert len(conflicts) == 1
    stored = await issuer.store.get(ticket_hash(issued.ticket))
    assert stored is not None
    assert stored.lease_token == claims[0].record.lease_token


async def test_database_retry_ticket_is_one_shared_bearer_across_service_instances(
    tmp_path,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'concurrent-retry-tickets.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    run = _run()
    await DatabaseRunRepository(session_factory).add_run(
        AgentRun(
            run_id=run.run_id,
            request_id="request-1",
            session_id="session-1",
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            turn_id=run.turn_id,
            plan_id=run.plan_id,
            step_id=run.step_id,
            status="running",
            invoker_type="delegated",
            delegated=True,
            state_version=run.state_version,
            event_sequence=run.event_sequence,
            deadline_at=run.deadline_at,
        )
    )
    now = datetime.now(UTC)
    first = ExecutionTicketService(
        DatabaseExecutionTicketStore(session_factory), secret="ticket-secret"
    )
    second = ExecutionTicketService(
        DatabaseExecutionTicketStore(session_factory), secret="ticket-secret"
    )

    issued = await asyncio.gather(
        first.issue(
            run,
            request_id="request-1",
            purpose="agent_event",
            ttl_seconds=120,
            now=now,
            reuse_active_for_run=True,
        ),
        second.issue(
            run,
            request_id="request-1",
            purpose="agent_event",
            ttl_seconds=120,
            now=now,
            reuse_active_for_run=True,
        ),
    )

    assert issued[0].ticket == issued[1].ticket
    active = await first.store.find_active(
        LegacyExecutionCorrelationQuery(
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="request-1",
            agent_id="agent-1",
            plan_id="plan-1",
            step_id="step-1",
            event_id="event-retry",
            purpose="agent_event",
            now=now,
        )
    )
    assert len(active) == 1
    assert active[0].ticket_hash == ticket_hash(issued[0].ticket)


async def test_database_issue_failure_fence_blocks_late_ticket_issue_for_the_same_run(
    tmp_path,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'failed-retry-ticket.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    run = _run()
    await DatabaseRunRepository(session_factory).add_run(
        AgentRun(
            run_id=run.run_id,
            request_id="request-1",
            session_id="session-1",
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            turn_id=run.turn_id,
            plan_id=run.plan_id,
            step_id=run.step_id,
            status="running",
            invoker_type="delegated",
            delegated=True,
            state_version=run.state_version,
            event_sequence=run.event_sequence,
            deadline_at=run.deadline_at,
        )
    )
    first = ExecutionTicketService(
        DatabaseExecutionTicketStore(session_factory), secret="ticket-secret"
    )
    second = ExecutionTicketService(
        DatabaseExecutionTicketStore(session_factory), secret="ticket-secret"
    )

    assert (
        await first.recover_after_issue_failure(
            run,
            purpose="agent_event",
        )
        is None
    )
    with pytest.raises(ExecutionTicketConflict, match="issuance"):
        await second.issue(
            run,
            request_id="request-1",
            purpose="agent_event",
            ttl_seconds=120,
            reuse_active_for_run=True,
        )


@pytest.mark.parametrize("finalizer", ["release", "consume", "progress"])
async def test_database_stale_worker_cannot_finalize_a_reclaimed_ticket(
    tmp_path, finalizer
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'stale-{finalizer}.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    issuer = ExecutionTicketService(
        DatabaseExecutionTicketStore(session_factory), secret="ticket-secret"
    )
    stale_store = DatabaseExecutionTicketStore(session_factory)
    stale_service = ExecutionTicketService(stale_store, secret="ticket-secret")
    current_service = ExecutionTicketService(
        DatabaseExecutionTicketStore(session_factory), secret="ticket-secret"
    )
    now = datetime.now(UTC)
    issued = await issuer.issue(
        _run(), request_id="request-1", purpose="agent_event", ttl_seconds=120, now=now
    )
    stale_claim = await stale_service.claim(
        issued.ticket,
        tenant_id="tenant-1",
        user_id="user-1",
        purpose="agent_event",
        owner="stale-worker",
        lease_seconds=10,
        now=now,
    )
    stale_token = stale_claim.record.lease_token
    assert stale_token is not None

    stale_read = asyncio.Event()
    reclaimed = asyncio.Event()
    original_get = stale_store.get

    async def pause_after_stale_read(ticket_hash_value: str):
        record = await original_get(ticket_hash_value)
        stale_read.set()
        await reclaimed.wait()
        return record

    stale_store.get = pause_after_stale_read  # type: ignore[method-assign]

    async def stale_finalize():
        if finalizer == "release":
            return await stale_service.release_claim(
                issued.ticket,
                owner="stale-worker",
                lease_token=stale_token,
                now=now + timedelta(seconds=9),
            )
        if finalizer == "progress":
            return await stale_service.release_after_progress(
                issued.ticket,
                owner="stale-worker",
                lease_token=stale_token,
                run_state_version=2,
                event_sequence=1,
                now=now + timedelta(seconds=9),
            )
        return await stale_service.consume(
            issued.ticket,
            event_id="event-stale",
            owner="stale-worker",
            lease_token=stale_token,
            now=now + timedelta(seconds=9),
        )

    stale_task = asyncio.create_task(stale_finalize())
    await stale_read.wait()
    current_claim = await current_service.claim(
        issued.ticket,
        tenant_id="tenant-1",
        user_id="user-1",
        purpose="agent_event",
        owner="current-worker",
        lease_seconds=30,
        now=now + timedelta(seconds=11),
    )
    reclaimed.set()
    outcome = (await asyncio.gather(stale_task, return_exceptions=True))[0]

    assert isinstance(outcome, ExecutionTicketError)
    stored = await issuer.store.get(ticket_hash(issued.ticket))
    assert stored is not None
    assert stored.status == ExecutionTicketStatus.CLAIMED
    assert stored.lease_owner == "current-worker"
    assert stored.lease_token == current_claim.record.lease_token


async def test_legacy_correlation_requires_exactly_one_trusted_mapping(tickets) -> None:
    now = datetime.now(UTC)
    query = LegacyExecutionCorrelationQuery(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-1",
        agent_id="agent-1",
        plan_id="plan-1",
        step_id="step-1",
        event_id="event-legacy",
        purpose="agent_event",
        now=now,
    )
    with pytest.raises(ExecutionTicketError, match="not unique"):
        await tickets.resolve_legacy(query)

    await tickets.issue(
        _run(), request_id="request-1", purpose="agent_event", ttl_seconds=60, now=now
    )
    matched = await tickets.resolve_legacy(query)
    assert matched.correlation_mode == "legacy_unique"
    assert matched.record.claims.run_id == "run-1"

    await tickets.issue(
        _run(), request_id="request-1", purpose="agent_event", ttl_seconds=60, now=now
    )
    with pytest.raises(ExecutionTicketError, match="not unique"):
        await tickets.resolve_legacy(query)
