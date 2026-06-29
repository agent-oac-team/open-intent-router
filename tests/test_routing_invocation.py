from app.schemas.invocation import InvokeRequest
from app.schemas.routing import RouteRequest
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.plan_service import PlanService
from app.services.router_service import RouterService


async def test_mock_router_returns_invocation_preview(settings, registry_service) -> None:
    service = RouterService(settings=settings, registry=registry_service)
    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize this text"},
            }
        )
    )
    assert response.decision.action == "open_agent"
    assert response.invocation
    assert response.invocation.input["text"] == "summarize this text"


async def test_mock_router_creates_and_persists_multi_agent_plan(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    plan_service = PlanService(repositories["plans"])
    service = RouterService(
        settings=settings,
        registry=registry_service,
        plan_service=plan_service,
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

    assert response.decision.action == "show_plan"
    assert response.context.relation == "multi_task"
    assert response.execution_policy == "require_confirmation"
    assert response.next_action
    assert response.next_action.type == "confirm_plan"
    assert response.plan
    assert response.plan.execution_policy == "require_confirmation"
    assert [step.agent_id for step in response.plan.steps] == ["summarizer", "task_creator"]
    assert response.plan.steps[1].depends_on == [response.plan.steps[0].step_id]
    assert await repositories["plans"].get(response.plan.plan_id)


async def test_mock_invocation_persists_run_and_result(settings, registry_service, repositories) -> None:
    service = InvocationService(
        registry=registry_service,
        run_repository=repositories["runs"],
        result_repository=repositories["results"],
        invokers=build_default_invoker_registry(settings),
    )
    result = await service.invoke(
        InvokeRequest(
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "roles": ["operator"]},
            input={"text": "hello"},
        )
    )
    assert result.status == "completed"
    assert await repositories["runs"].get_run(result.run_id)
    assert (await repositories["results"].list_recent("s1"))[0].run_id == result.run_id
