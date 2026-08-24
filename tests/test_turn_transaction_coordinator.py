from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.models import AgentResultModel, AgentRunModel, CanonicalTurnModel, TurnOutboxModel
from app.repositories.database import DatabasePlanRepository, DatabaseRunRepository
from app.repositories.turn_transactions import (
    DatabaseTurnTransactionCoordinator,
    TurnCompletionBundle,
    TurnTransactionConflict,
)
from app.repositories.turns import DatabaseTurnRepository
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.plans import Plan, PlanStep
from app.schemas.turns import (
    TurnOutboxEvent,
    TurnSemanticResponse,
    TurnStatus,
    TurnUserInput,
)
from app.services.plan_service import PlanService
from app.services.turn_service import TurnService


def _bound_plan(suffix: str) -> Plan:
    return Plan(
        plan_id=f"plan-{suffix}",
        session_id="session-1",
        user_id="user-1",
        tenant_id="tenant-1",
        steps=[
            PlanStep(
                step_id=f"step-{suffix}",
                agent_id="agent-1",
                description="Persist a frozen invocation Binding transactionally.",
                agent_revision=9,
                binding_requirement={
                    "kind": "invocation",
                    "adapter_key": "plan_adapter",
                    "connector_ref": "plan_connector",
                    "config": {"function": "execute"},
                },
            )
        ],
    )


async def _fixture(tmp_path, suffix: str, managed_database, *, with_plan: bool = False):
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'turn-transaction-{suffix}.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    turns = TurnService(DatabaseTurnRepository(factory))
    plan = (
        await PlanService(DatabasePlanRepository(factory)).save_plan(_bound_plan(suffix))
        if with_plan
        else None
    )
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
        plan_id=plan.plan_id if plan is not None else None,
    )
    await DatabaseRunRepository(factory).add_run(
        AgentRun(
            run_id=f"run-{suffix}",
            request_id=f"request-{suffix}",
            session_id="session-1",
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            turn_id=started.turn.turn_id,
            plan_id=plan.plan_id if plan is not None else None,
            step_id=plan.steps[0].step_id if plan is not None else None,
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
        turn_id=started.turn.turn_id,
        plan_id=plan.plan_id if plan is not None else None,
        step_id=plan.steps[0].step_id if plan is not None else None,
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
        turn_id=run.turn_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
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
    completed_plan = (
        plan.model_copy(
            update={
                "status": "completed",
                "current_step_id": None,
                "state_version": plan.state_version + 1,
                "steps": [plan.steps[0].model_copy(update={"status": "completed"})],
            }
        )
        if plan is not None
        else None
    )
    return (
        settings,
        factory,
        TurnCompletionBundle(
            run=run,
            result=result,
            turn=completed,
            outbox=outbox,
            plan=completed_plan,
        ),
    )


async def test_turn_completion_commits_run_result_turn_and_outbox_atomically(
    tmp_path, managed_database
) -> None:
    _settings, factory, bundle = await _fixture(tmp_path, "success", managed_database)

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


async def test_turn_completion_rolls_back_all_writes_when_outbox_fails(
    tmp_path, managed_database
) -> None:
    _settings, factory, bundle = await _fixture(
        tmp_path, "rollback", managed_database, with_plan=True
    )
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
    assert bundle.plan is not None
    restored = await PlanService(DatabasePlanRepository(factory)).get_plan(
        bundle.plan.plan_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert restored is not None
    assert restored.status == "pending"
    assert restored.steps[0].status == "pending"
    assert restored.steps[0].agent_revision == 9
    assert restored.steps[0].binding_requirement == bundle.plan.steps[0].binding_requirement


async def test_turn_completion_preserves_plan_binding_across_restart_and_unknown_commit_replay(
    tmp_path,
    managed_database,
) -> None:
    settings, factory, bundle = await _fixture(
        tmp_path, "bound-success", managed_database, with_plan=True
    )
    assert bundle.plan is not None

    await DatabaseTurnTransactionCoordinator(factory).complete(bundle)

    # A fresh repository simulates a worker restart after a successful commit.
    restarted_factory = await managed_database.session_factory(settings)
    restarted_plans = PlanService(DatabasePlanRepository(restarted_factory))
    restored = await restarted_plans.get_plan(
        bundle.plan.plan_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert restored is not None
    assert restored.status == "completed"
    assert restored.steps[0].status == "completed"
    assert restored.steps[0].agent_revision == 9
    assert restored.steps[0].binding_requirement == bundle.plan.steps[0].binding_requirement

    # A caller that lost the commit response retries the same stale bundle. It
    # must see the canonical version conflict rather than write a partial Plan
    # fence or a second outbox event.
    with pytest.raises(TurnTransactionConflict):
        await DatabaseTurnTransactionCoordinator(restarted_factory).complete(bundle)

    replayed = await restarted_plans.get_plan(
        bundle.plan.plan_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert replayed == restored
    async with restarted_factory() as session:
        outboxes = (
            (
                await session.execute(
                    select(TurnOutboxModel).where(TurnOutboxModel.turn_id == bundle.turn.turn_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(outboxes) == 1


async def test_turn_completion_rejects_a_stale_concurrent_plan_bundle(
    tmp_path, managed_database
) -> None:
    settings, factory, bundle = await _fixture(
        tmp_path, "bound-conflict", managed_database, with_plan=True
    )
    assert bundle.plan is not None
    competing = TurnCompletionBundle(
        run=bundle.run,
        result=bundle.result,
        turn=bundle.turn,
        plan=bundle.plan,
        outbox=bundle.outbox.model_copy(
            update={
                "outbox_id": "outbox-bound-conflict-contender",
                "idempotency_key": "turn-completed:bound-conflict-contender",
            }
        ),
    )

    # Both workers derived a bundle from the same pending Turn/Plan version.
    # Once one commits, the other stale bundle must be rejected before it can
    # overwrite the frozen Step or append its competing outbox event.
    await DatabaseTurnTransactionCoordinator(factory).complete(bundle)
    with pytest.raises(TurnTransactionConflict):
        await DatabaseTurnTransactionCoordinator(
            await managed_database.session_factory(settings)
        ).complete(competing)

    restored = await PlanService(DatabasePlanRepository(factory)).get_plan(
        bundle.plan.plan_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert restored is not None
    assert restored.status == "completed"
    assert restored.steps[0].agent_revision == 9
    assert restored.steps[0].binding_requirement == bundle.plan.steps[0].binding_requirement
