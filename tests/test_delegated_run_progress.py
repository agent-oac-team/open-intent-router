from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.database import DatabasePlanRepository
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunProgressStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunProgressStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import MemoryEventRepository, MemoryPlanRepository, MemoryRunRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunProgressCommand, DelegatedRunStartCommand
from app.schemas.plans import NextAction, Plan, PlanStep
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


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_agent_clarify_atomically_blocks_associated_plan_step(
    backend: str,
    tmp_path,
) -> None:
    plan_id = f"plan-clarify-{backend}"
    next_action = NextAction(
        type="wait_for_agent_event",
        message="waiting for delegated agent",
        plan_id=plan_id,
        step_id="step-1",
        agent_id="agent-1",
    )
    plan = Plan(
        plan_id=plan_id,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        status="running",
        current_step_id="step-1",
        next_action=next_action,
        steps=[
            PlanStep(
                step_id="step-1",
                agent_id="agent-1",
                description="delegated step",
                status="running",
            )
        ],
    )
    service, plans, started = await _delegated_plan_progress_runtime(
        backend,
        tmp_path,
        plan,
    )
    command = DelegatedRunProgressCommand(
        event_id=f"event-clarify-{backend}",
        event_type="agent_clarify",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id="tenant-1",
        user_id="user-1",
        agent_id="agent-1",
        plan_id=plan_id,
        step_id="step-1",
        expected_state_version=1,
        sequence=1,
        status="blocked",
        payload={"question": "Which account?"},
        occurred_at=datetime.now(UTC),
    )

    first = await service.progress(command)
    duplicate = await service.progress(command)

    stored = await plans.get(plan_id, tenant_id="tenant-1", user_id="user-1")
    assert first.duplicate is False and first.run.status == "blocked"
    assert duplicate.duplicate is True
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.current_step_id == "step-1"
    assert stored.steps[0].status == "blocked"
    assert stored.next_action == next_action
    assert stored.state_version == plan.state_version + 1
    assert stored.last_event_id == command.event_id


async def _delegated_plan_progress_runtime(
    backend: str,
    tmp_path,
    plan: Plan,
):
    if backend == "memory":
        runs = MemoryRunRepository()
        events = MemoryEventRepository()
        turns_repo = MemoryTurnRepository()
        plans = MemoryPlanRepository()
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns_repo,
                plan_repository=plans,
            ),
            MemoryDelegatedRunProgressStore(
                run_repository=runs,
                event_repository=events,
                plan_repository=plans,
            ),
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / f'{plan.plan_id}.db'}",
        )
        await create_all_tables(settings)
        factory = create_session_factory(settings)
        turns_repo = DatabaseTurnRepository(factory)
        plans = DatabasePlanRepository(factory)
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            DatabaseDelegatedRunProgressStore(factory),
        )

    await plans.save(plan)
    turn = await TurnService(turns_repo).start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id=plan.session_id,
        request_id=f"request-{plan.plan_id}",
        source="host",
        user_input=TurnUserInput(text="delegate"),
    )
    started = await service.start(
        DelegatedRunStartCommand(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id=plan.session_id,
            request_id=f"request-{plan.plan_id}",
            turn_id=turn.turn.turn_id,
            agent_id="agent-1",
            plan_id=plan.plan_id,
            step_id="step-1",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    return service, plans, started
