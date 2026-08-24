from app.api.router import route_and_execute
from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import UserContext
from app.schemas.plans import Plan
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
from app.schemas.security import NativePrincipal
from app.services.invocation_service import InvocationService
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from tests.support.v2_runtime import freeze_plan_bindings


def _user() -> UserContext:
    return UserContext(id="u1", roles=["operator"], attributes={"tenant_id": "t1"})


async def _save_v2_plan(repositories, registry_service, plan: Plan) -> Plan:
    frozen = freeze_plan_bindings(plan, registry_service, user=_user())
    return await repositories["plans"].save(frozen)


def test_route_response_accepts_plan_without_show_plan() -> None:
    response = RouteResponse(
        request_id="r1",
        session_id="s1",
        decision=RouteDecision(action="reply", message="这里是执行计划。"),
        context={"relation": "multi_task", "candidate_agent_ids": ["summarizer"]},
        execution_policy="require_confirmation",
        plan={
            "plan_id": "p1",
            "user_id": "u1",
            "tenant_id": "t1",
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
    await _save_v2_plan(
        repositories,
        registry_service,
        Plan.model_validate(
            {
                "plan_id": "p1",
                "user_id": "u1",
                "tenant_id": "t1",
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
        ),
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p1",
        user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "completed"
    assert [item["agent_id"] for item in response.results] == ["summarizer", "task_creator"]
    assert response.results[1]["status"] == "completed"
    assert (
        len(await repositories["results"].list_recent("s1", tenant_id="t1", user_id="u1", limit=10))
        == 2
    )


async def test_route_and_execute_uses_route_selected_definitions_without_registry_refetch(
    settings,
    registry_service,
    repositories,
) -> None:
    user = UserContext(
        id="u1",
        roles=["operator"],
        attributes={"tenant_id": "t1"},
    )
    selected = await registry_service.available_definitions(user)
    snapshot = registry_service.snapshot_runtime.snapshot
    assert snapshot is not None
    selected_bindings = {
        definition.agent_id: snapshot.select_for_user(definition.agent_id, user)
        for definition in selected
    }
    assert all(selected_bindings.values())
    plan = Plan.model_validate(
        {
            "plan_id": "candidate-set-plan",
            "user_id": "u1",
            "tenant_id": "t1",
            "session_id": "candidate-set-session",
            "status": "running",
            "steps": [
                {
                    "step_id": "candidate-set-step",
                    "agent_id": "summarizer",
                    "description": "summarize",
                }
            ],
        }
    )
    await _save_v2_plan(repositories, registry_service, plan)
    routed = (
        RouteResponse(
            request_id="candidate-set-request",
            session_id=plan.session_id,
            decision=RouteDecision(action="show_plan", message="execute"),
            context=RouteContext(candidate_agent_ids=[item.agent_id for item in selected]),
            execution_policy="auto_execute",
            plan=plan,
        )
        .bind_selected_definitions(selected)
        .bind_selected_bindings(selected_bindings)
    )

    class FixedRouter:
        async def route(self, _request):
            return routed

    class NoRegistryReads:
        snapshot_runtime = registry_service.snapshot_runtime
        binding_resolver = registry_service.binding_resolver

        async def available_definitions(self, _user):
            raise AssertionError("Route-and-Execute repeated Candidate Set selection")

        async def get_definition(self, agent_id):
            raise AssertionError(f"Route-and-Execute re-read {agent_id}")

    registry = NoRegistryReads()
    invocation = InvocationService(
        registry=registry,
        run_repository=repositories["runs"],
        result_repository=repositories["results"],
    )
    executor = PlanExecutor(
        plan_service=PlanService(repositories["plans"]),
        registry=registry,
        invocation_service=invocation,
    )

    response = await route_and_execute(
        RouteRequest.model_validate(
            {
                "request_id": "candidate-set-request",
                "session_id": plan.session_id,
                "user": user.model_dump(mode="json"),
                "input": {"text": "summarize this text"},
            }
        ),
        principal=NativePrincipal(
            claims_version="oir-principal-v1",
            subject="u1",
            tenant="t1",
            roles=["operator"],
        ),
        router_service=FixedRouter(),
        invocation_service=invocation,
        plan_executor=executor,
    )

    assert response.route.plan is not None
    assert response.route.plan.status == "completed"
    assert [item["agent_id"] for item in response.results] == ["summarizer"]


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
            "user_id": "u1",
            "tenant_id": "t1",
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
    await _save_v2_plan(repositories, registry_service, plan)

    assert (
        await repositories["results"].list_recent("s1", tenant_id="t1", user_id="u1", limit=10)
    ) == []
    saved = await repositories["plans"].get("p_return", tenant_id="t1", user_id="u1")
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
                "user": {
                    "id": "u1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
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
        AgentDefinitionV2.model_validate(
            {
                "schema_version": "oir-agent-v2",
                "agent_id": "dashboard",
                "name": "Dashboard",
                "description": "Open dashboard",
                "access_policy": {"allow_roles": ["operator"], "allow_tenants": ["*"]},
                "handling": {"kind": "ui_handoff", "route": "/dashboard"},
            }
        )
    )
    await registry_service.load()
    await _save_v2_plan(
        repositories,
        registry_service,
        Plan.model_validate(
            {
                "plan_id": "p_ui",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "s1",
                "status": "running",
                "steps": [{"step_id": "s1", "agent_id": "dashboard", "description": "open"}],
            }
        ),
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p_ui", user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}}
    )

    assert response.plan.status == "blocked"
    assert response.next_action
    assert response.next_action.type == "open_ui"
    assert response.next_action.route == "/dashboard"


async def test_plan_executor_pauses_for_missing_input(
    settings, registry_service, repositories
) -> None:
    await _save_v2_plan(
        repositories,
        registry_service,
        Plan.model_validate(
            {
                "plan_id": "p_missing",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "s1",
                "status": "running",
                "steps": [{"step_id": "s1", "agent_id": "summarizer", "description": "summarize"}],
            }
        ),
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p_missing", user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}}
    )

    assert response.plan.status == "blocked"
    assert response.next_action
    assert response.next_action.type == "collect_input"
    assert response.next_action.metadata["missing_inputs"] == ["text"]


async def test_plan_executor_resume_with_input(settings, registry_service, repositories) -> None:
    await _save_v2_plan(
        repositories,
        registry_service,
        Plan.model_validate(
            {
                "plan_id": "p_resume",
                "user_id": "u1",
                "tenant_id": "t1",
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
        ),
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p_resume",
        user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "completed"
    assert response.results[0]["agent_id"] == "summarizer"


async def test_plan_executor_marks_plan_failed_when_step_output_is_invalid(
    settings,
    registry_service,
    repositories,
) -> None:
    original_agent = registry_service.repository.agents["summarizer"]
    invalid_agent = original_agent.model_copy(
        update={
            "handling": original_agent.handling.model_copy(
                update={"config": {"function": "invalid"}}
            )
        }
    )
    await repositories["registry"].upsert(invalid_agent)
    await registry_service.load()
    await _save_v2_plan(
        repositories,
        registry_service,
        Plan.model_validate(
            {
                "plan_id": "p_invalid_output",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "s1",
                "status": "running",
                "steps": [{"step_id": "s1", "agent_id": "summarizer", "description": "summarize"}],
            }
        ),
    )
    executor = _executor(settings, registry_service, repositories)

    response = await executor.execute(
        "p_invalid_output",
        user={"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
        input_values={"text": "hello"},
    )

    assert response.plan.status == "failed"
    assert response.plan.current_step_id is None
    # The closed Runtime validates an Adapter outcome before it reaches the
    # service-level output projection. Its public failure status is therefore
    # the uniform Runtime failure shape.
    assert response.results[0]["status"] == "failed"
    assert response.results[0]["error"]["code"] == "invocation_invalid_response"


def _executor(settings, registry_service, repositories) -> PlanExecutor:
    invocation_service = InvocationService(
        registry=registry_service,
        run_repository=repositories["runs"],
        result_repository=repositories["results"],
    )
    return PlanExecutor(
        plan_service=PlanService(repositories["plans"]),
        registry=registry_service,
        invocation_service=invocation_service,
    )
