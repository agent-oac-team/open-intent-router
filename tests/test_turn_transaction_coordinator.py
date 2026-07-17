from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.models import AgentResultModel, AgentRunModel, CanonicalTurnModel, TurnOutboxModel
from app.db.session import create_all_tables, create_session_factory
from app.repositories.database import DatabaseRunRepository
from app.repositories.turn_transactions import (
    DatabaseTurnTransactionCoordinator,
    TurnCompletionBundle,
)
from app.repositories.turns import DatabaseTurnRepository
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.turns import (
    TurnOutboxEvent,
    TurnSemanticResponse,
    TurnStatus,
    TurnUserInput,
)
from app.services.turn_service import TurnService


async def _fixture(tmp_path, suffix: str):
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'turn-transaction-{suffix}.db'}",
    )
    await create_all_tables(settings)
    factory = create_session_factory(settings)
    turns = TurnService(DatabaseTurnRepository(factory))
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id=f"request-{suffix}",
        source="host",
        user_input=TurnUserInput(text="run task"),
    )
    await turns.attach_activity(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id=f"request-{suffix}",
        run_id=f"run-{suffix}",
    )
    await DatabaseRunRepository(factory).add_run(
        AgentRun(
            run_id=f"run-{suffix}",
            request_id=f"request-{suffix}",
            session_id="session-1",
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="delegated",
        )
    )
    active = await turns.repository.get(
        started.turn.turn_id, tenant_id="tenant-1", user_id="user-1"
    )
    assert active is not None
    now = datetime.now(UTC)
    references = active.references.model_copy(
        update={"result_ids": [f"result-{suffix}"]}, deep=True
    )
    completed = active.model_copy(
        update={
            "status": TurnStatus.COMPLETED,
            "state_version": active.state_version + 1,
            "references": references,
            "final_response": TurnSemanticResponse(kind="agent_result", text="done"),
            "updated_at": now,
            "completed_at": now,
        }
    )
    run = AgentRun(
        run_id=f"run-{suffix}",
        request_id=f"request-{suffix}",
        session_id="session-1",
        agent_id="agent-1",
        user_id="user-1",
        tenant_id="tenant-1",
        status="completed",
        invoker_type="delegated",
        output={"text": "done"},
        updated_at=now,
    )
    result = AgentResult(
        result_id=f"result-{suffix}",
        run_id=run.run_id,
        session_id=run.session_id,
        agent_id=run.agent_id,
        user_id=run.user_id,
        tenant_id=run.tenant_id,
        status="completed",
        message="done",
        output={"text": "done"},
        created_at=now,
    )
    outbox = TurnOutboxEvent(
        outbox_id=f"outbox-{suffix}",
        turn_id=completed.turn_id,
        event_type="turn.completed",
        idempotency_key=f"turn-completed:{completed.turn_id}",
        payload={"turn_id": completed.turn_id},
        available_at=now,
    )
    return factory, TurnCompletionBundle(run=run, result=result, turn=completed, outbox=outbox)


async def test_turn_completion_commits_run_result_turn_and_outbox_atomically(tmp_path) -> None:
    factory, bundle = await _fixture(tmp_path, "success")

    await DatabaseTurnTransactionCoordinator(factory).complete(bundle)

    async with factory() as session:
        run = await session.get(AgentRunModel, bundle.run.run_id)
        result = await session.get(AgentResultModel, bundle.result.result_id)
        turn = await session.get(CanonicalTurnModel, bundle.turn.turn_id)
        outbox = await session.get(TurnOutboxModel, bundle.outbox.outbox_id)
    assert run and run.status == "completed"
    assert result and result.status == "completed"
    assert turn and turn.status == "completed"
    assert outbox and outbox.status == "pending"


async def test_turn_completion_rolls_back_all_writes_when_outbox_fails(tmp_path) -> None:
    factory, bundle = await _fixture(tmp_path, "rollback")
    async with factory() as session:
        session.add(
            TurnOutboxModel(
                outbox_id="existing-outbox",
                turn_id=bundle.turn.turn_id,
                event_type="turn.completed",
                idempotency_key=bundle.outbox.idempotency_key,
                payload_text="{}",
                status="pending",
                attempt_count=0,
                max_attempts=5,
                available_at=datetime.now(UTC),
            )
        )
        await session.commit()

    with pytest.raises(IntegrityError):
        await DatabaseTurnTransactionCoordinator(factory).complete(bundle)

    async with factory() as session:
        run = await session.get(AgentRunModel, bundle.run.run_id)
        result = await session.get(AgentResultModel, bundle.result.result_id)
        turn = await session.get(CanonicalTurnModel, bundle.turn.turn_id)
        outboxes = (
            (
                await session.execute(
                    select(TurnOutboxModel).where(
                        TurnOutboxModel.idempotency_key == bundle.outbox.idempotency_key
                    )
                )
            )
            .scalars()
            .all()
        )
    assert run and run.status == "running"
    assert result is None
    assert turn and turn.status == "running"
    assert len(outboxes) == 1
