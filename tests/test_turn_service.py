import asyncio

import pytest

from app.core.config import Settings
from app.repositories.turns import (
    DatabaseTurnRepository,
    MemoryTurnRepository,
    TurnTerminalStateError,
)
from app.schemas.turns import TurnUserInput
from app.services.turn_service import TurnIdempotencyConflict, TurnService


@pytest.fixture(params=["memory", "database"])
async def turn_service(request, tmp_path, managed_database):
    if request.param == "memory":
        return TurnService(MemoryTurnRepository())
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'turn-service.db'}",
    )
    await managed_database.initialize_schema(settings)
    return TurnService(DatabaseTurnRepository(await managed_database.session_factory(settings)))


async def test_turn_service_returns_same_turn_for_identical_retry(turn_service) -> None:
    values = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "request_id": "request-1",
        "source": "host",
        "user_input": TurnUserInput(text="current request", metadata={"locale": "zh-CN"}),
    }

    first = await turn_service.start_turn(**values)
    replay = await turn_service.start_turn(**values)

    assert first.created is True
    assert replay.created is False
    assert replay.turn.turn_id == first.turn.turn_id


@pytest.mark.parametrize(
    "updates",
    [
        {"tenant_id": "tenant-2"},
        {"user_id": "user-2"},
        {"session_id": "session-2"},
        {"source": "api"},
        {"user_input": TurnUserInput(text="changed input")},
    ],
)
async def test_turn_service_rejects_request_identity_or_input_conflict(
    turn_service, updates
) -> None:
    values = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "request_id": "request-conflict",
        "source": "host",
        "user_input": TurnUserInput(text="original input"),
    }
    await turn_service.start_turn(**values)

    with pytest.raises(TurnIdempotencyConflict, match="conflicts"):
        await turn_service.start_turn(**{**values, **updates})


async def test_turn_service_active_run_result_lifecycle(turn_service) -> None:
    await turn_service.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-lifecycle",
        source="host",
        user_input=TurnUserInput(text="run task"),
    )

    active = await turn_service.attach_activity(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-lifecycle",
        run_id="run-1",
        plan_id="plan-1",
    )
    assert active.status.value == "running"
    assert active.references.run_ids == ["run-1"]
    assert active.references.plan_id == "plan-1"

    with pytest.raises(TurnIdempotencyConflict, match="Run"):
        await turn_service.complete_with_result(
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="request-lifecycle",
            run_id="run-other",
            result_id="result-other",
            response_text="wrong",
            plan_id="plan-1",
        )

    completed = await turn_service.complete_with_result(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-lifecycle",
        run_id="run-1",
        result_id="result-1",
        response_text="done",
        plan_id="plan-1",
    )
    assert completed.status.value == "completed"
    assert completed.references.result_ids == ["result-1"]
    assert completed.final_response and completed.final_response.text == "done"

    replay = await turn_service.complete_with_result(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-lifecycle",
        run_id="run-1",
        result_id="result-1",
        response_text="done",
        plan_id="plan-1",
    )
    assert replay.turn_id == completed.turn_id
    assert replay.state_version == completed.state_version
    assert replay.status == completed.status
    assert replay.references == completed.references
    assert replay.final_response == completed.final_response
    with pytest.raises(TurnTerminalStateError, match="terminal turn"):
        await turn_service.complete_with_result(
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="request-lifecycle",
            run_id="run-1",
            result_id="result-late",
            response_text="late conflict",
            plan_id="plan-1",
        )


async def _active_turn(turn_service, request_id: str) -> None:
    await turn_service.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id=request_id,
        source="host",
        user_input=TurnUserInput(text="concurrent task"),
    )
    await turn_service.attach_activity(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id=request_id,
        run_id="run-concurrent",
    )


async def test_concurrent_duplicate_final_results_converge(turn_service) -> None:
    await _active_turn(turn_service, "request-concurrent-same")

    results = await asyncio.gather(
        *[
            turn_service.complete_with_result(
                tenant_id="tenant-1",
                user_id="user-1",
                request_id="request-concurrent-same",
                run_id="run-concurrent",
                result_id="result-same",
                response_text="same result",
            )
            for _ in range(2)
        ]
    )

    assert results[0].turn_id == results[1].turn_id
    assert results[0].state_version == results[1].state_version
    assert results[0].references.result_ids == ["result-same"]


async def test_concurrent_conflicting_final_results_allow_only_one_winner(
    turn_service,
) -> None:
    await _active_turn(turn_service, "request-concurrent-conflict")

    results = await asyncio.gather(
        turn_service.complete_with_result(
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="request-concurrent-conflict",
            run_id="run-concurrent",
            result_id="result-a",
            response_text="result a",
        ),
        turn_service.complete_with_result(
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="request-concurrent-conflict",
            run_id="run-concurrent",
            result_id="result-b",
            response_text="result b",
        ),
        return_exceptions=True,
    )

    winners = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, TurnTerminalStateError)]
    assert len(winners) == 1
    assert len(conflicts) == 1
