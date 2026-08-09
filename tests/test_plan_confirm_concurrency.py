import asyncio

import pytest

from app.repositories.memory import MemoryPlanRepository
from app.schemas.plans import Plan, PlanStep
from app.services.plan_service import PlanService


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
