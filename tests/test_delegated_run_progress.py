from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunProgressStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunProgressStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import MemoryEventRepository, MemoryRunRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunProgressCommand, DelegatedRunStartCommand
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def delegated_progress(request, tmp_path):
    if request.param == "memory":
        runs = MemoryRunRepository()
        events = MemoryEventRepository()
        turn_repo = MemoryTurnRepository()
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turn_repo,
            ),
            MemoryDelegatedRunProgressStore(
                run_repository=runs,
                event_repository=events,
            ),
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-progress.db'}",
        )
        await create_all_tables(settings)
        factory = create_session_factory(settings)
        turn_repo = DatabaseTurnRepository(factory)
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            DatabaseDelegatedRunProgressStore(factory),
        )
    turn = await TurnService(turn_repo).start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host",
        user_input=TurnUserInput(text="delegate"),
    )
    started = await service.start(
        DelegatedRunStartCommand(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            request_id="request-1",
            turn_id=turn.turn.turn_id,
            agent_id="agent-1",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    return service, started


async def test_progress_event_is_ordered_and_idempotent(delegated_progress) -> None:
    service, started = delegated_progress
    command = DelegatedRunProgressCommand(
        event_id="event-1",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        expected_state_version=1,
        sequence=1,
        status="running",
        payload={"progress": 20},
        occurred_at=datetime.now(UTC),
    )

    first = await service.progress(command)
    duplicate = await service.progress(command)

    assert first.duplicate is False and first.run.state_version == 2
    assert duplicate.duplicate is True and duplicate.run.state_version == 2

    with pytest.raises(DelegatedRunStartConflict, match="order conflict"):
        await service.progress(
            command.model_copy(
                update={
                    "event_id": "event-out-of-order",
                    "expected_state_version": 2,
                }
            )
        )


async def test_progress_rejects_cross_owner_or_agent(delegated_progress) -> None:
    service, started = delegated_progress
    base = DelegatedRunProgressCommand(
        event_id="event-owner",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        expected_state_version=1,
        sequence=1,
        occurred_at=datetime.now(UTC),
    )

    for update in ({"user_id": "other-user"}, {"agent_id": "other-agent"}):
        with pytest.raises(DelegatedRunStartConflict, match="identity/order conflict"):
            await service.progress(base.model_copy(update=update))
