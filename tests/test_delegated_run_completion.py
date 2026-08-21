from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.repositories.database import DatabasePlanRepository
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunCompletionStore,
    DatabaseDelegatedRunStartStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunCompletionStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunCompleteCommand, DelegatedRunStartCommand
from app.schemas.plans import NextAction, Plan, PlanStep
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.turn_service import TurnService


@pytest.fixture(params=["memory", "database"])
async def delegated_completion(request, tmp_path, managed_database):
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
        await managed_database.initialize_schema(settings)
        factory = await managed_database.session_factory(settings)
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


@pytest.mark.parametrize(
    "updates",
    [
        {"result_id": "different-result"},
        {"response_text": "different answer"},
        {"output": {"answer": "different answer"}},
    ],
)
async def test_final_event_replay_requires_identical_canonical_payload(
    delegated_completion,
    updates: dict,
) -> None:
    service, _turns, command = delegated_completion
    await service.complete(command)

    with pytest.raises(DelegatedRunStartConflict, match="idempotency identity conflict"):
        await service.complete(command.model_copy(update=updates))


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_plan_completion_selects_first_dependency_ready_step(
    backend: str,
    tmp_path,
    managed_database,
) -> None:
    plan = Plan(
        plan_id=f"plan-ready-{backend}",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        status="running",
        current_step_id="current",
        steps=[
            PlanStep(
                step_id="blocked-pending",
                agent_id="agent-1",
                description="wait for future",
                depends_on=["future"],
            ),
            PlanStep(
                step_id="ready-pending",
                agent_id="agent-1",
                description="ready now",
            ),
            PlanStep(
                step_id="current",
                agent_id="agent-1",
                description="currently delegated",
                status="running",
            ),
            PlanStep(
                step_id="future",
                agent_id="agent-1",
                description="wait for ready",
                depends_on=["ready-pending"],
            ),
        ],
    )
    service, plans, command = await _delegated_plan_completion_runtime(
        backend,
        tmp_path,
        plan,
        managed_database,
        step_id="current",
    )

    await service.complete(command)

    stored = await plans.get(plan.plan_id, tenant_id="tenant-1", user_id="user-1")
    assert stored is not None
    assert stored.status == "running"
    assert stored.current_step_id == "ready-pending"
    assert {step.step_id: step.status for step in stored.steps} == {
        "blocked-pending": "pending",
        "ready-pending": "pending",
        "current": "completed",
        "future": "pending",
    }


@pytest.mark.parametrize("backend", ["memory", "database"])
@pytest.mark.parametrize("sibling_status", ["blocked", "running"])
async def test_plan_completion_does_not_complete_with_active_sibling(
    backend: str,
    sibling_status: str,
    tmp_path,
    managed_database,
) -> None:
    next_action = (
        NextAction(
            type="wait_for_agent_event",
            message="waiting",
            plan_id=f"plan-active-{backend}-{sibling_status}",
            step_id="sibling",
            agent_id="agent-1",
        )
        if sibling_status == "blocked"
        else None
    )
    plan = Plan(
        plan_id=f"plan-active-{backend}-{sibling_status}",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        status=sibling_status,
        current_step_id="current",
        next_action=next_action,
        steps=[
            PlanStep(
                step_id="current",
                agent_id="agent-1",
                description="currently delegated",
                status="running",
            ),
            PlanStep(
                step_id="sibling",
                agent_id="agent-1",
                description="still active",
                status=sibling_status,
            ),
        ],
    )
    service, plans, command = await _delegated_plan_completion_runtime(
        backend,
        tmp_path,
        plan,
        managed_database,
        step_id="current",
    )

    await service.complete(command)

    stored = await plans.get(plan.plan_id, tenant_id="tenant-1", user_id="user-1")
    assert stored is not None
    assert stored.status == sibling_status
    assert stored.current_step_id == "sibling"
    assert stored.steps[0].status == "completed"
    assert stored.steps[1].status == sibling_status
    assert stored.next_action == next_action


async def _delegated_plan_completion_runtime(
    backend: str,
    tmp_path,
    plan: Plan,
    managed_database,
    *,
    step_id: str,
):
    if backend == "memory":
        runs = MemoryRunRepository()
        results = MemoryResultRepository()
        events = MemoryEventRepository()
        turns_repo = MemoryTurnRepository()
        plans = MemoryPlanRepository()
        service = DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns_repo,
                plan_repository=plans,
            ),
            completion_store=MemoryDelegatedRunCompletionStore(
                run_repository=runs,
                result_repository=results,
                event_repository=events,
                turn_repository=turns_repo,
                outbox_repository=MemoryTurnOutboxRepository(),
                plan_repository=plans,
            ),
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / f'{plan.plan_id}.db'}",
        )
        await managed_database.initialize_schema(settings)
        factory = await managed_database.session_factory(settings)
        turns_repo = DatabaseTurnRepository(factory)
        plans = DatabasePlanRepository(factory)
        service = DelegatedRunService(
            DatabaseDelegatedRunStartStore(factory),
            completion_store=DatabaseDelegatedRunCompletionStore(factory),
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
            step_id=step_id,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    command = DelegatedRunCompleteCommand(
        event_id=f"event-{plan.plan_id}",
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        tenant_id="tenant-1",
        user_id="user-1",
        agent_id="agent-1",
        plan_id=plan.plan_id,
        step_id=step_id,
        expected_state_version=1,
        result_id=f"result-{plan.plan_id}",
        response_text="done",
        output={"answer": "done"},
        occurred_at=datetime.now(UTC),
    )
    return service, plans, command
