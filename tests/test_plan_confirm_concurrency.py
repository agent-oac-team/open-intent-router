import asyncio

import pytest

from app.core.config import Settings
from app.repositories.database import DatabasePlanRepository
from app.repositories.memory import MemoryPlanRepository
from app.schemas.plans import HostManagedStepCompletion, NextAction, Plan, PlanStep
from app.services.plan_service import PlanService, PlanStateConflict


@pytest.mark.asyncio
async def test_plan_confirm_is_idempotent_for_stale_and_terminal_clients() -> None:
    service = PlanService(MemoryPlanRepository())
    created = await service.save_plan(
        Plan(
            plan_id="plan-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            steps=[PlanStep(step_id="step-1", agent_id="agent-1", description="do it")],
        )
    )

    responses = await asyncio.gather(
        service.confirm(
            "plan-1",
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="confirm-1",
            expected_state_version=created.state_version,
        ),
        service.confirm(
            "plan-1",
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="confirm-2",
            expected_state_version=created.state_version,
        ),
    )
    confirmed = next(item for item in responses if item.transitioned)
    stale_replay = next(item for item in responses if not item.transitioned)

    assert confirmed.status == "running"
    assert confirmed.transitioned is True
    assert stale_replay.status == "running"
    assert stale_replay.transitioned is False
    assert stale_replay.state_version == confirmed.state_version

    canonical = await service.get_plan("plan-1", tenant_id="tenant-1", user_id="user-1")
    assert canonical is not None
    assert canonical.last_event_id in {"confirm-1", "confirm-2"}
    exact_replay = await service.confirm(
        "plan-1",
        tenant_id="tenant-1",
        user_id="user-1",
        request_id=canonical.last_event_id,
        expected_state_version=created.state_version,
    )
    assert exact_replay.transitioned is True
    assert exact_replay.status == "running"
    assert exact_replay.state_version == confirmed.state_version
    terminal = canonical.model_copy(
        update={
            "status": "completed",
            "current_step_id": None,
            "steps": [canonical.steps[0].model_copy(update={"status": "completed"})],
        }
    )
    terminal = await service.save_plan(terminal)
    completed_replay = await service.confirm(
        "plan-1",
        tenant_id="tenant-1",
        user_id="user-1",
        expected_state_version=confirmed.state_version,
    )

    assert completed_replay.status == "completed"
    assert completed_replay.transitioned is False
    assert completed_replay.current_step_id is None
    assert completed_replay.state_version == terminal.state_version


@pytest.mark.asyncio
async def test_host_managed_step_completion_advances_only_the_current_step_idempotently() -> None:
    service = PlanService(MemoryPlanRepository())
    created = await service.save_plan(
        Plan(
            plan_id="plan-host-step",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            status="blocked",
            current_step_id="step-1",
            next_action=NextAction(
                type="open_ui",
                plan_id="plan-host-step",
                step_id="step-1",
                agent_id="page-agent",
                route="/page-task",
            ),
            steps=[
                PlanStep(
                    step_id="step-1",
                    agent_id="page-agent",
                    description="finish the page task",
                    status="blocked",
                ),
                PlanStep(
                    step_id="step-2",
                    agent_id="next-agent",
                    description="continue the plan",
                ),
            ],
        )
    )

    advanced = await service.complete_host_managed_step(
        "plan-host-step",
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=HostManagedStepCompletion(
            request_id="page-task-completion-1",
            agent_id="page-agent",
            expected_state_version=created.state_version,
        ),
    )

    assert advanced.transitioned is True
    assert advanced.status == "running"
    assert advanced.current_step_id == "step-2"
    assert advanced.next_action is None

    replay = await service.complete_host_managed_step(
        "plan-host-step",
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=HostManagedStepCompletion(
            request_id="page-task-completion-1",
            agent_id="page-agent",
            expected_state_version=created.state_version,
        ),
    )
    assert replay.transitioned is True
    assert replay.state_version == advanced.state_version

    stale = await service.complete_host_managed_step(
        "plan-host-step",
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=HostManagedStepCompletion(
            request_id="page-task-completion-stale",
            agent_id="page-agent",
            expected_state_version=created.state_version,
        ),
    )
    assert stale.transitioned is False
    assert stale.state_version == advanced.state_version


@pytest.mark.asyncio
async def test_host_managed_step_completion_requires_the_current_ui_handoff_step() -> None:
    service = PlanService(MemoryPlanRepository())
    created = await service.save_plan(
        Plan(
            plan_id="plan-host-boundary",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            status="blocked",
            current_step_id="step-1",
            state_version=3,
            next_action=NextAction(
                type="open_ui",
                plan_id="plan-host-boundary",
                step_id="step-1",
                agent_id="page-agent",
                route="/page-task",
            ),
            steps=[
                PlanStep(
                    step_id="step-1",
                    agent_id="page-agent",
                    description="finish the page task",
                    status="blocked",
                ),
                PlanStep(
                    step_id="step-2",
                    agent_id="next-agent",
                    description="continue the plan",
                ),
            ],
        )
    )

    wrong_agent = await service.complete_host_managed_step(
        "plan-host-boundary",
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=HostManagedStepCompletion(
            request_id="page-task-wrong-agent",
            agent_id="other-agent",
            expected_state_version=created.state_version,
        ),
    )
    assert wrong_agent.transitioned is False
    assert wrong_agent.state_version == created.state_version

    wrong_handoff_agent = await service.save_plan(
        (
            await service.get_plan("plan-host-boundary", tenant_id="tenant-1", user_id="user-1")
        ).model_copy(
            update={
                "next_action": NextAction(
                    type="open_ui",
                    plan_id="plan-host-boundary",
                    step_id="step-1",
                    agent_id="other-agent",
                    route="/page-task",
                )
            }
        )
    )
    mismatched_handoff = await service.complete_host_managed_step(
        "plan-host-boundary",
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=HostManagedStepCompletion(
            request_id="page-task-mismatched-handoff",
            agent_id="page-agent",
            expected_state_version=wrong_handoff_agent.state_version,
        ),
    )
    assert mismatched_handoff.transitioned is False
    assert mismatched_handoff.state_version == wrong_handoff_agent.state_version

    waiting_for_external_execution = await service.save_plan(
        (
            await service.get_plan("plan-host-boundary", tenant_id="tenant-1", user_id="user-1")
        ).model_copy(
            update={
                "next_action": NextAction(
                    type="wait_for_agent_event",
                    plan_id="plan-host-boundary",
                    step_id="step-1",
                    agent_id="page-agent",
                )
            }
        )
    )
    wrong_handling = await service.complete_host_managed_step(
        "plan-host-boundary",
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=HostManagedStepCompletion(
            request_id="page-task-external-step",
            agent_id="page-agent",
            expected_state_version=waiting_for_external_execution.state_version,
        ),
    )
    assert wrong_handling.transitioned is False
    assert wrong_handling.state_version == waiting_for_external_execution.state_version


class _CommitAcknowledgementLossPlanRepository:
    """Lose the first acknowledgement only after the wrapped database commit succeeds."""

    def __init__(self, delegate: DatabasePlanRepository) -> None:
        self.delegate = delegate
        self.lose_next_acknowledgement = True

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    async def save_if_version(
        self, plan, *, expected_version: int, formation_suppressed: bool = False
    ):
        stored = await self.delegate.save_if_version(
            plan,
            expected_version=expected_version,
            formation_suppressed=formation_suppressed,
        )
        if stored is not None and self.lose_next_acknowledgement:
            self.lose_next_acknowledgement = False
            raise PlanStateConflict("commit acknowledgement lost")
        return stored


def _host_managed_plan(plan_id: str) -> Plan:
    return Plan(
        plan_id=plan_id,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        status="blocked",
        current_step_id="step-1",
        next_action=NextAction(
            type="open_ui",
            plan_id=plan_id,
            step_id="step-1",
            agent_id="page-agent",
            route="/page-task",
        ),
        steps=[
            PlanStep(
                step_id="step-1",
                agent_id="page-agent",
                description="finish the page task",
                status="blocked",
            ),
            PlanStep(
                step_id="step-2",
                agent_id="next-agent",
                description="continue the plan",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_host_managed_step_completion_is_transactional_across_concurrency_restart_and_ack_loss(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'host-managed-page-completion.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    service = PlanService(DatabasePlanRepository(session_factory))
    created = await service.save_plan(_host_managed_plan("plan-host-durable"), publish=False)

    completion = HostManagedStepCompletion(
        request_id="page-task-durable-1",
        agent_id="page-agent",
        expected_state_version=created.state_version,
    )
    first, duplicate = await asyncio.gather(
        service.complete_host_managed_step(
            created.plan_id,
            "step-1",
            tenant_id="tenant-1",
            user_id="user-1",
            completion=completion,
            publish=False,
        ),
        service.complete_host_managed_step(
            created.plan_id,
            "step-1",
            tenant_id="tenant-1",
            user_id="user-1",
            completion=completion,
            publish=False,
        ),
    )
    assert first.transitioned is True
    assert duplicate.transitioned is True
    assert first.state_version == duplicate.state_version

    restarted_factory = await managed_database.session_factory(settings)
    restarted = PlanService(DatabasePlanRepository(restarted_factory))
    replay = await restarted.complete_host_managed_step(
        created.plan_id,
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=completion,
        publish=False,
    )
    assert replay.transitioned is True
    assert replay.current_step_id == "step-2"
    assert replay.next_action is None

    loss_delegate = DatabasePlanRepository(session_factory)
    loss_service = PlanService(_CommitAcknowledgementLossPlanRepository(loss_delegate))
    ack_loss_created = await loss_service.save_plan(
        _host_managed_plan("plan-host-ack-loss"), publish=False
    )
    recovered = await loss_service.complete_host_managed_step(
        ack_loss_created.plan_id,
        "step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        completion=HostManagedStepCompletion(
            request_id="page-task-ack-loss-1",
            agent_id="page-agent",
            expected_state_version=ack_loss_created.state_version,
        ),
        publish=False,
    )
    assert recovered.transitioned is True
    assert recovered.current_step_id == "step-2"
    persisted = await restarted.get_plan(
        ack_loss_created.plan_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert persisted is not None
    assert persisted.last_event_id == "page-task-ack-loss-1"
    assert persisted.current_step_id == "step-2"
