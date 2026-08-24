from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import AgentResultModel, AgentRunModel, CanonicalTurnModel, TurnOutboxModel
from app.repositories.canonical_invocations import (
    DatabaseCanonicalInvocationStore,
    MemoryCanonicalInvocationStore,
)
from app.repositories.database import DatabaseResultRepository, DatabaseRunRepository
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.turns import FormationEligibilitySnapshot, TurnUserInput
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def canonical_store(request, tmp_path, managed_database):
    if request.param == "memory":
        runs = MemoryRunRepository()
        results = MemoryResultRepository()
        turns = MemoryTurnRepository()
        outbox = MemoryTurnOutboxRepository()
        return (
            MemoryCanonicalInvocationStore(
                run_repository=runs,
                result_repository=results,
                turn_repository=turns,
                outbox_repository=outbox,
            ),
            TurnService(turns),
            runs,
            results,
            turns,
            outbox,
        )
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'canonical-invocation.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    return (
        DatabaseCanonicalInvocationStore(factory),
        TurnService(DatabaseTurnRepository(factory)),
        DatabaseRunRepository(factory),
        DatabaseResultRepository(factory),
        DatabaseTurnRepository(factory),
        factory,
    )


async def test_canonical_invocation_store_closes_turn_and_persists_outbox(
    canonical_store,
) -> None:
    store, turns, _, _, _, backend = canonical_store
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host_chat",
        user_input=TurnUserInput(text="prepare visit; I like pork"),
    )
    now = datetime.now(UTC)
    run, active_turn, replay = await store.start_run(
        AgentRun(
            run_id="run-1",
            request_id="request-1",
            session_id="session-1",
            agent_id="visit-preparation",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
            created_at=now,
            updated_at=now,
        )
    )
    assert replay is None
    assert run.turn_id == started.turn.turn_id
    assert active_turn.status.value == "running"

    completed_run, result, completed_turn = await store.complete_run(
        run=run.model_copy(update={"status": "completed", "output": {"ok": True}}),
        result=AgentResult(
            result_id="result-1",
            run_id=run.run_id,
            session_id=run.session_id,
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            status="completed",
            message="visit prepared",
            output={"ok": True},
            created_at=now,
        ),
        response_text="visit prepared",
        eligibility=FormationEligibilitySnapshot(
            mode="enforced", policy_version="formation-policy-v1"
        ),
    )
    assert completed_run.status == "completed"
    assert result.turn_id == completed_turn.turn_id
    assert completed_turn.status.value == "completed"
    assert completed_turn.references.run_ids == ["run-1"]
    assert completed_turn.references.result_ids == ["result-1"]

    if isinstance(backend, MemoryTurnOutboxRepository):
        assert len(backend.events) == 1
        event = next(iter(backend.events.values()))
        assert event.payload["formation_eligibility"]["mode"] == "enforced"
    else:
        async with backend() as session:
            stored_run = await session.get(AgentRunModel, "run-1")
            stored_result = await session.get(AgentResultModel, "result-1")
            stored_turn = await session.get(CanonicalTurnModel, completed_turn.turn_id)
            outbox = (await session.execute(select(TurnOutboxModel))).scalar_one()
        assert stored_run and stored_run.status == "completed"
        assert stored_result and stored_result.turn_id == completed_turn.turn_id
        assert stored_turn and stored_turn.status == "completed"
        assert outbox.payload_text.find('"mode":"enforced"') >= 0


async def test_canonical_invocation_store_replays_terminal_result(canonical_store) -> None:
    store, turns, _, _, _, _ = canonical_store
    await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-replay",
        source="host_chat",
        user_input=TurnUserInput(text="same request"),
    )
    now = datetime.now(UTC)
    run, _, _ = await store.start_run(
        AgentRun(
            run_id="run-first",
            request_id="request-replay",
            session_id="session-1",
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
            created_at=now,
            updated_at=now,
        )
    )
    await store.complete_run(
        run=run.model_copy(update={"status": "completed"}),
        result=AgentResult(
            result_id="result-first",
            run_id=run.run_id,
            session_id=run.session_id,
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            status="completed",
            message="done",
            created_at=now,
        ),
        response_text="done",
        eligibility=FormationEligibilitySnapshot(
            mode="enforced", policy_version="formation-policy-v1"
        ),
    )

    replay_run, replay_turn, replay_result = await store.start_run(
        run.model_copy(update={"run_id": "run-second", "turn_id": None})
    )
    assert replay_run.run_id == "run-first"
    assert replay_turn.status.value == "completed"
    assert replay_result and replay_result.result_id == "result-first"
