from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunFailureStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunFailureStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import MemoryEventRepository, MemoryRunRepository
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunFailCommand, DelegatedRunStartCommand
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def delegated_failure(request, tmp_path, managed_database):
    if request.param == "memory":
        runs = MemoryRunRepository()
        events = MemoryEventRepository()
        turns_repo = MemoryTurnRepository()
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns_repo,
            ),
            failure_store=MemoryDelegatedRunFailureStore(
                run_repository=runs,
                event_repository=events,
                turn_repository=turns_repo,
                outbox_repository=MemoryTurnOutboxRepository(),
            ),
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-failure.db'}",
        )
        await managed_database.initialize_schema(settings)
        factory = await managed_database.session_factory(settings)
        turns_repo = DatabaseTurnRepository(factory)
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            failure_store=DatabaseDelegatedRunFailureStore(factory),
        )

    turn = await TurnService(turns_repo).start_turn(
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
    command = DelegatedRunFailCommand(
        event_id="event-failure",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        expected_state_version=1,
        occurred_at=datetime.now(UTC),
        error={"code": "provider_execution_failed"},
    )
    return service, turns_repo, command


async def test_failure_event_atomically_terminates_run_and_turn(delegated_failure) -> None:
    service, turns, command = delegated_failure

    failed = await service.fail(command)
    duplicate = await service.fail(command)
    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )

    assert failed.duplicate is False
    assert duplicate.duplicate is True
    assert failed.run.status.value == "failed"
    assert turn and turn.status.value == "failed"
    assert turn.final_response and turn.final_response.error == {
        "code": "provider_execution_failed"
    }


async def test_failure_event_rejects_cross_owner_without_partial_terminal_state(
    delegated_failure,
) -> None:
    service, turns, command = delegated_failure

    with pytest.raises(DelegatedRunStartConflict, match="identity/state conflict"):
        await service.fail(command.model_copy(update={"user_id": "other-user"}))

    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    assert turn and turn.status.value == "running"
    assert turn.final_response is None
