from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunCompletionStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunCompletionStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunCompleteCommand, DelegatedRunStartCommand
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def delegated_completion(request, tmp_path):
    if request.param == "memory":
        runs = MemoryRunRepository()
        results = MemoryResultRepository()
        events = MemoryEventRepository()
        turns_repo = MemoryTurnRepository()
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns_repo,
            ),
            completion_store=MemoryDelegatedRunCompletionStore(
                run_repository=runs,
                result_repository=results,
                event_repository=events,
                turn_repository=turns_repo,
                outbox_repository=MemoryTurnOutboxRepository(),
            ),
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-completion.db'}",
        )
        await create_all_tables(settings)
        factory = create_session_factory(settings)
        turns_repo = DatabaseTurnRepository(factory)
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            completion_store=DatabaseDelegatedRunCompletionStore(factory),
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
    command = DelegatedRunCompleteCommand(
        event_id="event-final",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        expected_state_version=1,
        result_id="result-final",
        response_text="final answer",
        output={"answer": "final answer"},
        occurred_at=datetime.now(UTC),
    )
    return service, turns_repo, command


async def test_final_event_atomically_completes_run_result_turn_and_outbox(
    delegated_completion,
) -> None:
    service, turns, command = delegated_completion

    completed = await service.complete(command)
    duplicate = await service.complete(command)
    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )

    assert completed.duplicate is False
    assert duplicate.duplicate is True
    assert completed.result_id == duplicate.result_id == "result-final"
    assert completed.run.status.value == "completed"
    assert turn and turn.status.value == "completed"
    assert turn.references.result_ids == ["result-final"]
    assert turn.final_response and turn.final_response.text == "final answer"


async def test_final_event_rejects_cross_owner_without_partial_completion(
    delegated_completion,
) -> None:
    service, turns, command = delegated_completion

    with pytest.raises(DelegatedRunStartConflict, match="identity/state conflict"):
        await service.complete(command.model_copy(update={"user_id": "other-user"}))

    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    assert turn and turn.status.value == "running"
    assert turn.references.result_ids == []
