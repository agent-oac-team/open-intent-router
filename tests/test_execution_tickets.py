from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.execution_tickets import (
    DatabaseExecutionTicketStore,
    MemoryExecutionTicketStore,
    ticket_hash,
)
from app.schemas.delegated_runs import DelegatedRunReference, DelegatedRunStatus
from app.schemas.execution_tickets import (
    ExecutionTicketStatus,
    LegacyExecutionCorrelationQuery,
)
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
