from app.schemas.agents import AgentDefinition
from app.schemas.plans import Plan
from app.schemas.routing import RouteDecision, RouteResponse
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService


def test_route_response_accepts_plan_without_show_plan() -> None:
    response = RouteResponse(
        request_id="r1",
        session_id="s1",
        decision=RouteDecision(action="reply", message="这里是执行计划。"),
        context={"relation": "multi_task", "candidate_agent_ids": ["summarizer"]},
        execution_policy="require_confirmation",
        plan={
            "plan_id": "p1",
            "session_id": "s1",
            "steps": [{"step_id": "s1", "agent_id": "summarizer", "description": "summarize"}],
        },
    )

    assert response.plan
    assert response.decision.action == "reply"


async def test_plan_executor_confirmed_plan_invokes_steps_in_dependency_order(
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
                "plan_id": "p1",
                "session_id": "s1",
                "status": "running",
                "steps": [
                    {"step_id": "s1", "agent_id": "summarizer", "description": "summarize"},
                    {
                        "step_id": "s2",
                        "agent_id": "task_creator",
                        "description": "create task",
                        "depends_on": ["s1"],
                    },
                ],
            }
        )
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p1",
        user={"id": "u1", "roles": ["operator"]},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "completed"
    assert [item["agent_id"] for item in response.results] == ["summarizer", "task_creator"]
    assert response.results[1]["status"] == "completed"
    assert len(await repositories["results"].list_recent("s1", limit=10)) == 2


async def test_return_plan_only_policy_does_not_invoke_until_executor_called(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    plan = Plan.model_validate(
        {
            "plan_id": "p_return",
            "session_id": "s1",
            "execution_policy": "return_plan_only",
            "steps": [
                {"step_id": "s1", "agent_id": "summarizer", "description": "summarize"},
                {
                    "step_id": "s2",
                    "agent_id": "task_creator",
                    "description": "create task",
                    "depends_on": ["s1"],
                },
            ],
        }
    )
    await repositories["plans"].save(plan)

    assert (await repositories["results"].list_recent("s1", limit=10)) == []
    saved = await repositories["plans"].get("p_return")
    assert saved
    assert saved.execution_policy == "return_plan_only"


async def test_auto_execute_policy_can_be_applied_in_local_settings(
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    from app.core.config import Settings
    from app.schemas.routing import RouteRequest
    from app.services.router_service import RouterService

    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        router_llm_provider="mock",
        default_plan_execution_policy="auto_execute",
        allow_local_auto_execute_plans=True,
    )
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        plan_service=PlanService(repositories["plans"]),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "first summarize this text, then create a task"},
            }
        )
    )

    assert response.execution_policy == "auto_execute"
    assert response.plan
    assert response.plan.execution_policy == "auto_execute"


async def test_plan_executor_pauses_for_ui_handoff(
    settings,
    registry_service,
    repositories,
) -> None:
    await repositories["registry"].upsert(
        AgentDefinition.model_validate(
            {
                "agent_id": "dashboard",
                "name": "Dashboard",
                "description": "Open dashboard",
                "type": "ui_handoff",
                "access_policy": {"allow_roles": ["operator"], "allow_tenants": ["*"]},
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {"route": {"type": "string"}}},
                "invocation": {"type": "ui_handoff", "config": {}},
                "ui_handoff": {"mode": "host_route", "route": "/dashboard", "params": {}},
            }
        )
    )
    await registry_service.load()
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "p_ui",
                "session_id": "s1",
                "status": "running",
                "steps": [{"step_id": "s1", "agent_id": "dashboard", "description": "open"}],
            }
        )
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute("p_ui", user={"id": "u1", "roles": ["operator"]})

    assert response.plan.status == "blocked"
    assert response.next_action
    assert response.next_action.type == "open_ui"
    assert response.next_action.route == "/dashboard"


async def test_plan_executor_pauses_for_missing_input(
    settings, registry_service, repositories
) -> None:
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "p_missing",
                "session_id": "s1",
                "status": "running",
                "steps": [{"step_id": "s1", "agent_id": "summarizer", "description": "summarize"}],
            }
        )
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute("p_missing", user={"id": "u1", "roles": ["operator"]})

    assert response.plan.status == "blocked"
    assert response.next_action
    assert response.next_action.type == "collect_input"
    assert response.next_action.metadata["missing_inputs"] == ["text"]


async def test_plan_executor_resume_with_input(settings, registry_service, repositories) -> None:
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "p_resume",
                "session_id": "s1",
                "status": "blocked",
                "steps": [
                    {
                        "step_id": "s1",
                        "agent_id": "summarizer",
                        "description": "summarize",
                        "status": "blocked",
                    }
                ],
            }
        )
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p_resume",
        user={"id": "u1", "roles": ["operator"]},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "completed"
    assert response.results[0]["agent_id"] == "summarizer"


async def test_plan_executor_marks_plan_failed_when_step_output_is_invalid(
    settings,
    registry_service,
    repositories,
) -> None:
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "p_invalid_output",
                "session_id": "s1",
                "status": "running",
                "steps": [{"step_id": "s1", "agent_id": "summarizer", "description": "summarize"}],
            }
        )
    )
    original_agent = registry_service.repository.agents["summarizer"]
    invalid_agent = registry_service.repository.agents["summarizer"].model_copy(
        update={
            "invocation": original_agent.invocation.model_copy(
                update={"config": {"response": {"summary": 123}}}
            )
        }
    )
    await repositories["registry"].upsert(invalid_agent)
    await registry_service.load()
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p_invalid_output",
        user={"id": "u1", "roles": ["operator"]},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "failed"
    assert response.plan.current_step_id == "s1"
    assert response.results[0]["status"] == "invalid_output"
    assert response.results[0]["error"]["code"] == "invalid_output"


def _executor(settings, registry_service, repositories) -> PlanExecutor:
    invocation_service = InvocationService(
        registry=registry_service,
        run_repository=repositories["runs"],
        result_repository=repositories["results"],
        invokers=build_default_invoker_registry(settings),
    )
    return PlanExecutor(
        plan_service=PlanService(repositories["plans"]),
        registry=registry_service,
        invocation_service=invocation_service,
    )
