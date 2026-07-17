from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunMaintenanceStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunMaintenanceStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import MemoryEventRepository, MemoryRunRepository
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import (
    DelegatedRunOrphanQuery,
    DelegatedRunStartCommand,
    DelegatedRunTimeoutCommand,
)
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def maintenance(request, tmp_path):
    now = datetime.now(UTC)
    if request.param == "memory":
        runs = MemoryRunRepository()
        events = MemoryEventRepository()
        turns_repo = MemoryTurnRepository()
        store = MemoryDelegatedRunMaintenanceStore(
            run_repository=runs,
            event_repository=events,
            turn_repository=turns_repo,
            outbox_repository=MemoryTurnOutboxRepository(),
        )
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns_repo,
            ),
            maintenance_store=store,
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-maintenance.db'}",
        )
        await create_all_tables(settings)
        factory = create_session_factory(settings)
        turns_repo = DatabaseTurnRepository(factory)
        store = DatabaseDelegatedRunMaintenanceStore(factory)
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            maintenance_store=store,
        )
    turn = await TurnService(turns_repo).start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host",
        user_input=TurnUserInput(text="deadline task"),
    )
    deadline = now - timedelta(seconds=1)
    started = await service.start(
        DelegatedRunStartCommand(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            request_id="request-1",
            turn_id=turn.turn.turn_id,
            agent_id="agent-1",
            deadline_at=deadline,
        )
    )
    return service, turns_repo, started, now, deadline


async def test_orphan_query_and_timeout_converge_without_completed_result(maintenance) -> None:
    service, turns, started, now, deadline = maintenance

    orphans = await service.list_orphans(
        DelegatedRunOrphanQuery(
            now=now,
            stale_before=now - timedelta(minutes=10),
            tenant_id="tenant-1",
        )
    )
    assert [run.run_id for run in orphans.runs] == [started.run.run_id]

    command = DelegatedRunTimeoutCommand(
        event_id="event-timeout",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        expected_state_version=1,
        occurred_at=now,
        deadline_at=deadline,
    )
    timed_out = await service.timeout(command)
    duplicate = await service.timeout(command)
    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )

    assert timed_out.run.status.value == "timed_out"
    assert duplicate.duplicate is True
    assert turn and turn.status.value == "timed_out"
    assert turn.final_response is None
    remaining = await service.list_orphans(
        DelegatedRunOrphanQuery(
            now=now,
            stale_before=now - timedelta(minutes=10),
            tenant_id="tenant-1",
        )
    )
    assert remaining.runs == []


async def test_timeout_before_deadline_or_cross_owner_is_rejected(maintenance) -> None:
    service, turns, started, now, deadline = maintenance
    base = DelegatedRunTimeoutCommand(
        event_id="event-invalid-timeout",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        expected_state_version=1,
        occurred_at=deadline - timedelta(seconds=1),
        deadline_at=deadline,
    )

    with pytest.raises(DelegatedRunStartConflict, match="deadline conflict"):
        await service.timeout(base)
    with pytest.raises(DelegatedRunStartConflict, match="identity/deadline conflict"):
        await service.timeout(base.model_copy(update={"occurred_at": now, "user_id": "other-user"}))
    turn = await turns.get(
        base.turn_id,
        tenant_id=base.tenant_id,
        user_id=base.user_id,
    )
    assert turn and turn.status.value == "running"
