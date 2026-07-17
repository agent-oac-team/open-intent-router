from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.turns import (
    DatabaseTurnRepository,
    MemoryTurnRepository,
    TurnOwnershipConflict,
    TurnTerminalStateError,
)
from app.schemas.turns import CanonicalTurn, TurnSemanticResponse, TurnStatus, TurnUserInput


def _turn(**updates) -> CanonicalTurn:
    now = datetime.now(UTC)
    values = {
        "turn_id": "turn-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "request_id": "request-1",
        "source": "host",
        "status": "pending",
        "state_version": 1,
        "user_input": {"text": "hello"},
        "created_at": now,
        "updated_at": now,
    }
    return CanonicalTurn.model_validate({**values, **updates})


@pytest.fixture(params=["memory", "database"])
async def turn_repository(request, tmp_path):
    if request.param == "memory":
        return MemoryTurnRepository()
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'turn-repository.db'}",
    )
    await create_all_tables(settings)
    return DatabaseTurnRepository(create_session_factory(settings))


async def test_turn_repository_idempotent_create_and_owned_reads(turn_repository) -> None:
    original = _turn()

    created, was_created = await turn_repository.create_idempotent(original)
    replayed, replay_created = await turn_repository.create_idempotent(
        _turn(turn_id="ignored-replay-id")
    )

    assert was_created is True
    assert replay_created is False
    assert created.turn_id == replayed.turn_id == "turn-1"
    assert await turn_repository.get("turn-1", tenant_id="tenant-1", user_id="user-1")
    assert await turn_repository.get("turn-1", tenant_id="tenant-1", user_id="other") is None
    assert await turn_repository.get_by_request(
        tenant_id="tenant-1", user_id="user-1", request_id="request-1"
    )


async def test_turn_repository_conditional_update_and_terminal_protection(turn_repository) -> None:
    original, _ = await turn_repository.create_idempotent(_turn())
    running = original.model_copy(
        update={"status": TurnStatus.RUNNING, "state_version": 2, "updated_at": datetime.now(UTC)}
    )

    updated = await turn_repository.update_if_version(running, expected_version=1)
    stale = await turn_repository.update_if_version(
        running.model_copy(update={"state_version": 3}), expected_version=1
    )
    assert updated and updated.status == TurnStatus.RUNNING
    assert stale is None

    now = datetime.now(UTC)
    completed = running.model_copy(
        update={
            "status": TurnStatus.COMPLETED,
            "state_version": 3,
            "final_response": TurnSemanticResponse(kind="reply", text="done"),
            "updated_at": now,
            "completed_at": now,
        }
    )
    assert await turn_repository.update_if_version(completed, expected_version=2)
    with pytest.raises(TurnTerminalStateError, match="terminal turn"):
        await turn_repository.update_if_version(
            completed.model_copy(
                update={
                    "status": TurnStatus.RUNNING,
                    "state_version": 4,
                    "final_response": None,
                    "completed_at": None,
                }
            ),
            expected_version=3,
        )


async def test_turn_repository_rejects_turn_id_reuse_for_another_request(
    turn_repository,
) -> None:
    await turn_repository.create_idempotent(_turn())

    with pytest.raises(TurnOwnershipConflict, match="turn_id"):
        await turn_repository.create_idempotent(
            _turn(request_id="request-2", session_id="session-2")
        )


def test_turn_user_input_remains_semantic_not_host_history() -> None:
    turn = _turn(user_input=TurnUserInput(text="only current semantic input"))

    assert turn.user_input.text == "only current semantic input"
    assert "history" not in turn.user_input.model_dump()
