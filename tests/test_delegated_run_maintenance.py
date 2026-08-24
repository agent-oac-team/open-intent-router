from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.repositories.database import DatabasePlanRepository
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunMaintenanceStore,
    DatabaseDelegatedRunProgressStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunMaintenanceStore,
    MemoryDelegatedRunProgressStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryPlanRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import (
    DelegatedRunOrphanQuery,
    DelegatedRunProgressCommand,
    DelegatedRunStartCommand,
    DelegatedRunTimeoutCommand,
)
from app.schemas.plans import Plan, PlanStep
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.delegated_run_timeout_runtime import timeout_event_id
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def maintenance(request, tmp_path, managed_database):
    now = datetime.now(UTC)
    if request.param == "memory":
        runs = MemoryRunRepository()
        events = MemoryEventRepository()
        turns_repo = MemoryTurnRepository()
        plans = MemoryPlanRepository()
        store = MemoryDelegatedRunMaintenanceStore(
            run_repository=runs,
            event_repository=events,
            turn_repository=turns_repo,
            outbox_repository=MemoryTurnOutboxRepository(),
            plan_repository=plans,
        )
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns_repo,
                plan_repository=plans,
            ),
            progress_store=MemoryDelegatedRunProgressStore(
                run_repository=runs,
                event_repository=events,
                plan_repository=plans,
            ),
            maintenance_store=store,
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-maintenance.db'}",
        )
        await managed_database.initialize_schema(settings)
        factory = await managed_database.session_factory(settings)
        turns_repo = DatabaseTurnRepository(factory)
        plans = DatabasePlanRepository(factory)
        store = DatabaseDelegatedRunMaintenanceStore(factory)
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            progress_store=DatabaseDelegatedRunProgressStore(factory),
            maintenance_store=store,
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
                    description="deadline task",
                    status="running",
                )
            ],
        )
    )
    turn = await TurnService(turns_repo).start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host",
        user_input=TurnUserInput(text="deadline task"),
    )
    deadline = now - timedelta(seconds=1)
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
            deadline_at=deadline,
        )
    )
    return service, turns_repo, plans, started, now, deadline


async def test_orphan_query_and_timeout_converge_without_completed_result(maintenance) -> None:
    service, turns, plans, started, now, deadline = maintenance

    orphans = await service.list_orphans(
        DelegatedRunOrphanQuery(
            now=now,
            stale_before=now - timedelta(minutes=10),
            tenant_id="tenant-1",
        )
    )
    assert [run.run_id for run in orphans.runs] == [started.run.run_id]

    command = DelegatedRunTimeoutCommand(
        event_id="event-timeout",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        plan_id=started.run.plan_id,
        step_id=started.run.step_id,
        expected_state_version=1,
        occurred_at=now,
        deadline_at=deadline,
    )
    timed_out = await service.timeout(command)
    duplicate = await service.timeout(command)
    turn = await turns.get(
        command.turn_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    plan = await plans.get("plan-1", tenant_id="tenant-1", user_id="user-1")

    assert timed_out.run.status.value == "timed_out"
    assert duplicate.duplicate is True
    assert turn and turn.status.value == "timed_out"
    assert turn.final_response is None
    assert plan and plan.status == "failed" and plan.steps[0].status == "failed"
    remaining = await service.list_orphans(
        DelegatedRunOrphanQuery(
            now=now,
            stale_before=now - timedelta(minutes=10),
            tenant_id="tenant-1",
        )
    )
    assert remaining.runs == []


async def test_progress_cannot_preempt_the_timeout_event_namespace(maintenance) -> None:
    service, _, _, started, now, deadline = maintenance
    reserved_event_id = timeout_event_id(started.run)
    progress = DelegatedRunProgressCommand(
        event_id=reserved_event_id,
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        plan_id=started.run.plan_id,
        step_id=started.run.step_id,
        expected_state_version=1,
        sequence=1,
        occurred_at=now,
    )

    with pytest.raises(DelegatedRunStartConflict, match="reserved for maintenance"):
        await service.progress(progress)

    timed_out = await service.timeout(
        DelegatedRunTimeoutCommand(
            event_id=reserved_event_id,
            run_id=started.run.run_id,
            turn_id=started.run.turn_id,
            tenant_id=started.run.tenant_id,
            user_id=started.run.user_id,
            agent_id=started.run.agent_id,
            plan_id=started.run.plan_id,
            step_id=started.run.step_id,
            expected_state_version=1,
            occurred_at=now,
            deadline_at=deadline,
        )
    )
    assert timed_out.duplicate is False
    assert timed_out.run.status.value == "timed_out"


async def test_timeout_duplicate_requires_canonical_timeout_event_identity(maintenance) -> None:
    service, _, _, started, now, deadline = maintenance
    collision_id = "event-non-timeout-collision"
    await service.progress(
        DelegatedRunProgressCommand(
            event_id=collision_id,
            run_id=started.run.run_id,
            turn_id=started.run.turn_id,
            tenant_id=started.run.tenant_id,
            user_id=started.run.user_id,
            agent_id=started.run.agent_id,
            plan_id=started.run.plan_id,
            step_id=started.run.step_id,
            expected_state_version=1,
            sequence=1,
            occurred_at=now,
        )
    )

    with pytest.raises(DelegatedRunStartConflict, match="idempotency conflict"):
        await service.timeout(
            DelegatedRunTimeoutCommand(
                event_id=collision_id,
                run_id=started.run.run_id,
                turn_id=started.run.turn_id,
                tenant_id=started.run.tenant_id,
                user_id=started.run.user_id,
                agent_id=started.run.agent_id,
                plan_id=started.run.plan_id,
                step_id=started.run.step_id,
                expected_state_version=2,
                occurred_at=now,
                deadline_at=deadline,
            )
        )


async def test_timeout_before_deadline_or_cross_owner_is_rejected(maintenance) -> None:
    service, turns, plans, started, now, deadline = maintenance
    base = DelegatedRunTimeoutCommand(
        event_id="event-invalid-timeout",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id=started.run.tenant_id,
        user_id=started.run.user_id,
        agent_id=started.run.agent_id,
        plan_id=started.run.plan_id,
        step_id=started.run.step_id,
        expected_state_version=1,
        occurred_at=deadline - timedelta(seconds=1),
        deadline_at=deadline,
    )

    with pytest.raises(DelegatedRunStartConflict, match="deadline conflict"):
        await service.timeout(base)
    with pytest.raises(DelegatedRunStartConflict, match="identity/deadline conflict"):
        await service.timeout(base.model_copy(update={"occurred_at": now, "user_id": "other-user"}))
    turn = await turns.get(
        base.turn_id,
        tenant_id=base.tenant_id,
        user_id=base.user_id,
    )
    plan = await plans.get("plan-1", tenant_id="tenant-1", user_id="user-1")
    assert turn and turn.status.value == "running"
    assert plan and plan.status == "running" and plan.steps[0].status == "running"
