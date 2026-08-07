import pytest
from pydantic import ValidationError

from app.schemas.agents import AgentDefinition
from app.schemas.plans import Plan
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService, PlanStateConflict


def test_plan_initializes_current_step_from_first_ready_step() -> None:
    plan = Plan.model_validate(
        {
            "plan_id": "plan-initial-ready",
            "user_id": "u1",
            "tenant_id": "t1",
            "steps": [
                {
                    "step_id": "step-3",
                    "agent_id": "summarizer",
                    "description": "third",
                    "depends_on": ["step-2"],
                },
                {
                    "step_id": "step-2",
                    "agent_id": "summarizer",
                    "description": "second",
                    "depends_on": ["step-1"],
                },
                {"step_id": "step-1", "agent_id": "summarizer", "description": "first"},
            ],
        }
    )

    assert plan.current_step_id == "step-1"


async def test_executor_ignores_non_ready_current_step_and_runs_unordered_dag(
    settings,
    registry_service,
    repositories,
) -> None:
    plan = Plan.model_validate(
        {
            "plan_id": "plan-unordered",
            "user_id": "u1",
            "tenant_id": "t1",
            "session_id": "session-1",
            "status": "running",
            "current_step_id": "step-3",
            "steps": [
                {
                    "step_id": "step-3",
                    "agent_id": "summarizer",
                    "description": "third",
                    "depends_on": ["step-2"],
                },
                {
                    "step_id": "step-2",
                    "agent_id": "summarizer",
                    "description": "second",
                    "depends_on": ["step-1"],
                },
                {
                    "step_id": "step-1",
                    "agent_id": "summarizer",
                    "description": "first",
                },
            ],
        }
    )
    await repositories["plans"].save(plan)

    response = await _executor(settings, registry_service, repositories).execute(
        plan.plan_id,
        user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "completed"
    assert [item["step_id"] for item in response.results] == ["step-1", "step-2", "step-3"]
    assert all(step.status == "completed" for step in response.plan.steps)


async def test_multiple_ready_steps_run_serially_in_original_list_order(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "plan-stable-ready",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "session-1",
                "status": "running",
                "steps": [
                    {"step_id": "step-b", "agent_id": "task_creator", "description": "B"},
                    {"step_id": "step-a", "agent_id": "summarizer", "description": "A"},
                    {
                        "step_id": "step-c",
                        "agent_id": "summarizer",
                        "description": "C",
                        "depends_on": ["step-a", "step-b"],
                    },
                ],
            }
        )
    )

    response = await _executor(settings, registry_service, repositories).execute(
        "plan-stable-ready",
        user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
        input_values={"text": "hello", "title": "todo"},
    )

    assert [item["step_id"] for item in response.results] == ["step-b", "step-a", "step-c"]
    assert response.plan.status == "completed"


async def test_blocked_dependency_prevents_dependent_invocation(
    settings,
    registry_service,
    repositories,
) -> None:
    await repositories["registry"].upsert(
        AgentDefinition.model_validate(
            {
                "agent_id": "dashboard",
                "name": "Dashboard",
                "description": "Open a dashboard",
                "type": "ui_handoff",
                "access_policy": {"allow_roles": ["operator"]},
                "invocation": {"type": "ui_handoff", "config": {}},
                "ui_handoff": {"mode": "host_route", "route": "/dashboard"},
            }
        )
    )
    await registry_service.load()
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "plan-blocked-dependency",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "session-1",
                "status": "running",
                "steps": [
                    {
                        "step_id": "dependent",
                        "agent_id": "summarizer",
                        "description": "must wait",
                        "depends_on": ["handoff"],
                    },
                    {
                        "step_id": "handoff",
                        "agent_id": "dashboard",
                        "description": "open UI",
                    },
                ],
            }
        )
    )

    response = await _executor(settings, registry_service, repositories).execute(
        "plan-blocked-dependency",
        user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
        input_values={"text": "hello"},
    )

    statuses = {step.step_id: step.status for step in response.plan.steps}
    assert response.plan.status == "blocked"
    assert response.plan.current_step_id == "handoff"
    assert statuses == {"dependent": "pending", "handoff": "blocked"}
    assert response.results == []


@pytest.mark.parametrize(
    "steps",
    [
        [
            {"step_id": "same", "agent_id": "summarizer", "description": "one"},
            {"step_id": "same", "agent_id": "summarizer", "description": "two"},
        ],
        [
            {
                "step_id": "one",
                "agent_id": "summarizer",
                "description": "one",
                "depends_on": ["missing"],
            }
        ],
        [
            {
                "step_id": "one",
                "agent_id": "summarizer",
                "description": "one",
                "depends_on": ["two"],
            },
            {
                "step_id": "two",
                "agent_id": "summarizer",
                "description": "two",
                "depends_on": ["one"],
            },
        ],
    ],
)
def test_plan_rejects_invalid_graphs(steps) -> None:
    with pytest.raises(ValidationError):
        Plan.model_validate(
            {"plan_id": "invalid", "user_id": "u1", "tenant_id": "t1", "steps": steps}
        )


async def test_step_failure_is_fail_fast_and_leaves_other_steps_pending(
    settings,
    registry_service,
    repositories,
) -> None:
    original = repositories["registry"].agents["summarizer"]
    await repositories["registry"].upsert(
        original.model_copy(
            update={
                "invocation": original.invocation.model_copy(
                    update={"config": {"response": {"summary": 123}}}
                )
            }
        )
    )
    await registry_service.load()
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "plan-fail-fast",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "session-1",
                "status": "running",
                "steps": [
                    {"step_id": "failing", "agent_id": "summarizer", "description": "fail"},
                    {"step_id": "never", "agent_id": "summarizer", "description": "wait"},
                ],
            }
        )
    )

    response = await _executor(settings, registry_service, repositories).execute(
        "plan-fail-fast",
        user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "failed"
    assert [step.status for step in response.plan.steps] == ["failed", "pending"]
    assert [item["step_id"] for item in response.results] == ["failing"]


@pytest.mark.parametrize(
    "plan",
    [
        Plan.model_validate(
            {
                "plan_id": "plan-false-complete",
                "user_id": "u1",
                "tenant_id": "t1",
                "status": "completed",
                "steps": [
                    {"step_id": "pending", "agent_id": "summarizer", "description": "pending"}
                ],
            }
        ),
        Plan.model_validate(
            {
                "plan_id": "plan-no-ready",
                "user_id": "u1",
                "tenant_id": "t1",
                "status": "running",
                "steps": [
                    {
                        "step_id": "cancelled-parent",
                        "agent_id": "summarizer",
                        "description": "cancelled",
                        "status": "cancelled",
                    },
                    {
                        "step_id": "pending-child",
                        "agent_id": "summarizer",
                        "description": "pending",
                        "depends_on": ["cancelled-parent"],
                    },
                ],
            }
        ),
    ],
)
async def test_incomplete_plan_without_ready_step_raises_invariant_conflict(
    settings,
    registry_service,
    repositories,
    plan: Plan,
) -> None:
    await repositories["plans"].save(plan)

    with pytest.raises(PlanStateConflict):
        await _executor(settings, registry_service, repositories).execute(
            plan.plan_id,
            user={
                "id": "u1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "t1"},
            },
            input_values={"text": "hello"},
        )

    stored = await repositories["plans"].get(plan.plan_id, tenant_id="t1", user_id="u1")
    assert stored == plan


def _executor(settings, registry_service, repositories) -> PlanExecutor:
    plans = PlanService(repositories["plans"])
    return PlanExecutor(
        plan_service=plans,
        registry=registry_service,
        invocation_service=InvocationService(
            registry=registry_service,
            run_repository=repositories["runs"],
            result_repository=repositories["results"],
            invokers=build_default_invoker_registry(settings),
            plan_service=plans,
        ),
    )
