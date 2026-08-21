from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.models import TurnOutboxModel
from app.repositories.database import DatabaseEventRepository, DatabasePlanRepository
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunCancelStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunCancelStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryPlanRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunCancelCommand, DelegatedRunStartCommand
from app.schemas.plans import Plan, PlanStep
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.turn_service import TurnService


@dataclass
class CancelRuntime:
    service: DelegatedRunService
    started: object
    turns: object
    plans: object
    events: object
    outbox: MemoryTurnOutboxRepository | None
    session_factory: object | None

    async def outbox_count(self) -> int:
        if self.outbox is not None:
            return len(self.outbox.events)
        async with self.session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(TurnOutboxModel)))


@pytest.fixture(params=["memory", "database"])
async def cancel_runtime(request, tmp_path, managed_database) -> CancelRuntime:
    if request.param == "memory":
        runs = MemoryRunRepository()
        events = MemoryEventRepository()
        turns = MemoryTurnRepository()
        plans = MemoryPlanRepository()
        outbox = MemoryTurnOutboxRepository()
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns,
                plan_repository=plans,
            ),
            cancel_store=MemoryDelegatedRunCancelStore(
                run_repository=runs,
                event_repository=events,
                turn_repository=turns,
                outbox_repository=outbox,
                plan_repository=plans,
            ),
        )
        session_factory = None
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-cancel.db'}",
        )
        await managed_database.initialize_schema(settings)
        session_factory = await managed_database.session_factory(settings)
        events = DatabaseEventRepository(session_factory)
        turns = DatabaseTurnRepository(session_factory)
        plans = DatabasePlanRepository(session_factory)
        outbox = None
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(session_factory),
            cancel_store=DatabaseDelegatedRunCancelStore(session_factory),
        )
    await plans.save(
        Plan(
            plan_id="plan-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            status="running",
            state_version=1,
            steps=[
                PlanStep(
                    step_id="step-1",
                    agent_id="agent-1",
                    description="delegate",
                    status="running",
                )
            ],
        )
    )
    turn = await TurnService(turns).start_turn(
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
            plan_id="plan-1",
            step_id="step-1",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    return CancelRuntime(service, started, turns, plans, events, outbox, session_factory)


async def test_cancel_confirmation_atomically_converges_and_is_idempotent(
    cancel_runtime: CancelRuntime,
) -> None:
    runtime = cancel_runtime
    run = runtime.started.run
    command = DelegatedRunCancelCommand(
        event_id="event-cancel",
        run_id=run.run_id,
        turn_id=run.turn_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        agent_id=run.agent_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
        expected_state_version=1,
        occurred_at=datetime.now(UTC),
        reason="user_requested",
    )

    first = await runtime.service.cancel(command)
    duplicate = await runtime.service.cancel(command)
    turn = await runtime.turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    plan = await runtime.plans.get(
        command.plan_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    event = await runtime.events.get_event(
        command.event_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )

    assert first.duplicate is False and first.run.status == "cancelled"
    assert duplicate.duplicate is True and duplicate.run.state_version == 2
    assert turn and turn.status == "cancelled" and turn.completed_at is not None
    assert plan and plan.status == "cancelled" and plan.steps[0].status == "cancelled"
    assert event and event.event_type == "agent_cancelled" and event.status == "cancelled"
    assert await runtime.outbox_count() == 1


@pytest.mark.parametrize(
    "update",
    [
        {"user_id": "other-user"},
        {"expected_state_version": 2},
    ],
)
async def test_cancel_rejects_identity_or_version_without_partial_transition(
    cancel_runtime: CancelRuntime,
    update: dict,
) -> None:
    runtime = cancel_runtime
    run = runtime.started.run
    command = DelegatedRunCancelCommand(
        event_id="event-invalid-cancel",
        run_id=run.run_id,
        turn_id=run.turn_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        agent_id=run.agent_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
        expected_state_version=1,
        occurred_at=datetime.now(UTC),
        reason="user_requested",
    ).model_copy(update=update)

    with pytest.raises(DelegatedRunStartConflict):
        await runtime.service.cancel(command)

    turn = await runtime.turns.get(
        run.turn_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
    )
    plan = await runtime.plans.get(
        run.plan_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
    )
    assert turn and turn.status == "running"
    assert plan and plan.status == "running" and plan.steps[0].status == "running"
    assert await runtime.outbox_count() == 0


async def test_different_cancel_event_cannot_overwrite_terminal_state(
    cancel_runtime: CancelRuntime,
) -> None:
    runtime = cancel_runtime
    run = runtime.started.run
    first = DelegatedRunCancelCommand(
        event_id="event-first-cancel",
        run_id=run.run_id,
        turn_id=run.turn_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        agent_id=run.agent_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
        expected_state_version=1,
        occurred_at=datetime.now(UTC),
        reason="user_requested",
    )
    await runtime.service.cancel(first)

    with pytest.raises(DelegatedRunStartConflict):
        await runtime.service.cancel(
            first.model_copy(
                update={
                    "event_id": "event-second-cancel",
                    "expected_state_version": 2,
                }
            )
        )

    assert await runtime.outbox_count() == 1
