from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import MemoryRunRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunStartCommand
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def delegated_start(request, tmp_path, managed_database):
    if request.param == "memory":
        runs = MemoryRunRepository()
        turns_repo = MemoryTurnRepository()
        turns = TurnService(turns_repo)
        store = MemoryDelegatedRunStartStore(
            run_repository=runs,
            turn_repository=turns_repo,
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-start.db'}",
        )
        await managed_database.initialize_schema(settings)
        factory = await managed_database.session_factory(settings)
        turns_repo = DatabaseTurnRepository(factory)
        turns = TurnService(turns_repo)
        store = DatabaseDelegatedRunStartStore(factory)
        runs = None
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host",
        user_input=TurnUserInput(text="delegate task"),
    )
    command = DelegatedRunStartCommand(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        turn_id=started.turn.turn_id,
        agent_id="agent-1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        input={"query": "delegate task"},
    )
    return DelegatedRunService(store), turns_repo, runs, command


async def test_delegated_run_is_persisted_before_safe_handoff(delegated_start) -> None:
    service, turns, runs, command = delegated_start

    first = await service.start(command)
    replay = await service.start(command)
    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )

    assert first.accepted is True and first.duplicate is False
    assert replay.duplicate is True and replay.run.run_id == first.run.run_id
    assert first.run.status.value == "pending"
    assert turn and first.run.run_id in turn.references.run_ids
    assert turn.status.value == "running"
    if runs is not None:
        stored = await runs.get_run(first.run.run_id)
        assert stored and stored.delegated is True


async def test_delegated_run_start_failure_returns_no_handoff_or_orphan(delegated_start) -> None:
    service, turns, runs, command = delegated_start
    forged = command.model_copy(update={"user_id": "other-user"})

    with pytest.raises(DelegatedRunStartConflict):
        await service.start(forged)

    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    assert turn and turn.references.run_ids == []
    if runs is not None:
        assert runs.runs == {}
