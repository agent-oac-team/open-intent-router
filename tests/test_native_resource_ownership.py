import pytest

from app.core.config import Settings, get_settings
from app.dependencies import (
    get_chat_history_service,
    get_invocation_service,
    get_plan_executor,
    get_plan_service,
    get_registry_service,
    get_repository_bundle,
    get_router_service,
)
from app.invokers.registry import AgentInvokerRegistry
from app.main import create_app
from app.repositories.database import DatabaseRunRepository
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
    MemoryMessageRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.schemas.agents import AgentDefinition
from app.schemas.invocation import AgentInvocationResult
from app.schemas.logs import AgentRun
from app.schemas.plans import Plan, PlanExecutionResponse
from app.schemas.routing import LLMRouteInput, RouteContext, RouteDecision, RouteResponse
from app.schemas.sessions import ChatMessage
from app.services.chat_history_service import ChatHistoryService
from app.services.invocation_service import InvocationService
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from app.services.registry_service import AgentRegistryService
from app.services.router_service import RouterService
from tests.fakes.native_principal import native_principal_headers

_PRINCIPAL_SECRET = "native-resource-test-secret"


async def test_native_run_read_is_owner_scoped_without_admin_bypass(
    non_lifespan_test_client,
) -> None:
    runs = MemoryRunRepository()
    await runs.add_run(
        AgentRun(
            run_id="run-owned",
            session_id="session-1",
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
        )
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_repository_bundle] = lambda: {"runs": runs}
    client = non_lifespan_test_client(app)

    owner = client.get("/api/v1/runs/run-owned", headers=_headers())
    other_user = client.get(
        "/api/v1/runs/run-owned",
        headers=_headers(subject="user-2"),
    )
    other_tenant = client.get(
        "/api/v1/runs/run-owned",
        headers=_headers(tenant="tenant-2"),
    )
    admin = client.get(
        "/api/v1/runs/run-owned",
        headers=_headers(subject="admin", roles=["admin"]),
    )
    missing_identity = client.get("/api/v1/runs/run-owned")

    assert owner.status_code == 200
    assert [response.status_code for response in (other_user, other_tenant, admin)] == [
        404,
        404,
        404,
    ]
    assert missing_identity.status_code == 401


async def test_native_session_messages_use_principal_owner_and_hide_foreign_history(
    non_lifespan_test_client,
) -> None:
    messages = MemoryMessageRepository()
    history = ChatHistoryService(messages, host_limit=20, agent_limit=12)
    await history.record(
        ChatMessage(
            message_id="message-1",
            session_id="session-owned",
            user_id="user-1",
            tenant_id="tenant-1",
            source="host_chat",
            role="user",
            content="owner message",
        )
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_chat_history_service] = lambda: history
    client = non_lifespan_test_client(app)

    owner = client.get(
        "/api/v1/sessions/session-owned/messages?user_id=user-2&tenant_id=tenant-2",
        headers=_headers(),
    )
    other_user = client.get(
        "/api/v1/sessions/session-owned/messages",
        headers=_headers(subject="user-2"),
    )
    other_tenant = client.get(
        "/api/v1/sessions/session-owned/messages",
        headers=_headers(tenant="tenant-2"),
    )
    admin = client.get(
        "/api/v1/sessions/session-owned/messages",
        headers=_headers(subject="admin", roles=["admin"]),
    )
    forged_append = client.post(
        "/api/v1/sessions/session-owned/messages",
        headers=_headers(),
        json={
            "source": "host_chat",
            "role": "assistant",
            "content": "forged owner",
            "user_id": "user-2",
            "tenant_id": "tenant-1",
        },
    )
    appended = client.post(
        "/api/v1/sessions/session-owned/messages",
        headers=_headers(),
        json={
            "source": "host_chat",
            "role": "assistant",
            "content": "trusted owner",
        },
    )

    assert owner.status_code == 200
    assert [item["content"] for item in owner.json()["messages"]] == ["owner message"]
    assert other_user.status_code == other_tenant.status_code == admin.status_code == 404
    assert forged_append.status_code == 401
    assert appended.status_code == 200
    assert (appended.json()["tenant_id"], appended.json()["user_id"]) == (
        "tenant-1",
        "user-1",
    )


async def test_native_plan_access_uses_principal_and_rejects_body_owner_conflict(
    non_lifespan_test_client,
) -> None:
    plans = PlanService(MemoryPlanRepository())
    await plans.save_plan(
        Plan(
            plan_id="plan-owned",
            session_id="session-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            steps=[
                {
                    "step_id": "step-1",
                    "agent_id": "agent-1",
                    "description": "run",
                }
            ],
        )
    )
    executor = _CapturingPlanExecutor()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_plan_service] = lambda: plans
    app.dependency_overrides[get_plan_executor] = lambda: executor
    client = non_lifespan_test_client(app)

    owner = client.get("/api/v1/plans/plan-owned", headers=_headers())
    other = client.get(
        "/api/v1/plans/plan-owned",
        headers=_headers(subject="user-2"),
    )
    admin = client.get(
        "/api/v1/plans/plan-owned",
        headers=_headers(subject="admin", roles=["admin"]),
    )
    conflict = client.post(
        "/api/v1/plans/plan-owned/execute",
        headers=_headers(),
        json={
            "user": {"id": "user-2", "attributes": {"tenant_id": "tenant-1"}},
            "input": {},
            "context": {},
        },
    )

    assert owner.status_code == 200
    assert other.status_code == admin.status_code == 404
    assert conflict.status_code == 401
    assert executor.calls == 0


async def test_public_agent_catalog_stays_public_but_available_agents_use_principal(
    non_lifespan_test_client,
) -> None:
    definitions = MemoryAgentDefinitionRepository()
    await definitions.upsert(
        AgentDefinition.model_validate(
            {
                "agent_id": "operator-agent",
                "name": "Operator Agent",
                "description": "Only operators may use this Agent.",
                "type": "mock",
                "access_policy": {"allow_roles": ["operator"]},
                "invocation": {"type": "mock", "config": {}},
            }
        )
    )
    registry = AgentRegistryService(
        Settings(storage_backend="memory", registry_backend="database"),
        repository=definitions,
    )
    await registry.load()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_registry_service] = lambda: registry
    client = non_lifespan_test_client(app)
    body = {
        "user": {
            "id": "user-1",
            "roles": ["operator"],
            "attributes": {"tenant_id": "tenant-1"},
        }
    }

    public = client.get("/api/v1/agents")
    missing_identity = client.post("/api/v1/agents/available", json=body)
    trusted_operator = client.post(
        "/api/v1/agents/available",
        headers=_headers(roles=["operator"]),
        json={**body, "user": {**body["user"], "roles": []}},
    )
    forged_operator = client.post(
        "/api/v1/agents/available",
        headers=_headers(),
        json=body,
    )

    assert public.status_code == 200
    assert public.json()["agents"][0]["agent_id"] == "operator-agent"
    assert missing_identity.status_code == 401
    assert trusted_operator.status_code == 200
    assert trusted_operator.json()["available_agents"] == ["operator-agent"]
    assert forged_operator.status_code == 200
    assert forged_operator.json()["available_agents"] == []


async def test_direct_invoke_filters_before_execution_side_effects(
    non_lifespan_test_client,
) -> None:
    definitions = MemoryAgentDefinitionRepository()
    await definitions.upsert(
        AgentDefinition.model_validate(
            {
                "agent_id": "operator-agent",
                "name": "Operator Agent",
                "description": "Only operators may invoke this Agent.",
                "type": "mock",
                "access_policy": {"allow_roles": ["operator"]},
                "invocation": {"type": "mock", "config": {}},
            }
        )
    )
    registry = AgentRegistryService(
        Settings(storage_backend="memory", registry_backend="database"),
        repository=definitions,
    )
    await registry.load()
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invoker = _RecordingInvoker()
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    invocation = InvocationService(
        registry=registry,
        run_repository=runs,
        result_repository=results,
        invokers=invokers,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_invocation_service] = lambda: invocation
    client = non_lifespan_test_client(app)
    body = {
        "session_id": "session-1",
        "agent_id": "operator-agent",
        "user": {
            "id": "user-1",
            "roles": ["operator"],
            "attributes": {"tenant_id": "tenant-1"},
        },
        "input": {},
    }

    denied = client.post("/api/v1/invoke", headers=_headers(), json=body)

    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "agent_not_available"
    assert invoker.calls == 0
    assert runs.runs == {}
    assert results.results == []

    allowed = client.post(
        "/api/v1/invoke",
        headers=_headers(roles=["operator"]),
        json={**body, "user": {**body["user"], "roles": []}},
    )

    assert allowed.status_code == 200
    assert allowed.json()["status"] == "completed"
    assert invoker.calls == 1


async def test_plan_request_preflights_one_candidate_set_before_any_invocation(
    non_lifespan_test_client,
) -> None:
    definitions = MemoryAgentDefinitionRepository()
    for agent_id, role in (("first-agent", "operator"), ("revoked-agent", "admin")):
        await definitions.upsert(
            AgentDefinition.model_validate(
                {
                    "agent_id": agent_id,
                    "name": agent_id,
                    "description": f"Agent requiring {role}.",
                    "type": "mock",
                    "access_policy": {"allow_roles": [role]},
                    "invocation": {"type": "mock", "config": {}},
                }
            )
        )
    registry = _CountingRegistry(
        Settings(storage_backend="memory", registry_backend="database"),
        repository=definitions,
    )
    await registry.load()
    plans = PlanService(MemoryPlanRepository())
    original = await plans.save_plan(
        Plan(
            plan_id="plan-preflight",
            session_id="session-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            steps=[
                {
                    "step_id": "step-1",
                    "agent_id": "first-agent",
                    "description": "first",
                },
                {
                    "step_id": "step-2",
                    "agent_id": "revoked-agent",
                    "description": "second",
                    "depends_on": ["step-1"],
                },
            ],
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invoker = _RecordingInvoker()
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    invocation = InvocationService(
        registry=registry,
        run_repository=runs,
        result_repository=results,
        invokers=invokers,
        plan_service=plans,
    )
    executor = PlanExecutor(
        plan_service=plans,
        registry=registry,
        invocation_service=invocation,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_plan_executor] = lambda: executor
    client = non_lifespan_test_client(app)

    response = client.post(
        "/api/v1/plans/plan-preflight/execute",
        headers=_headers(roles=["operator"]),
        json={
            "user": {"id": "user-1", "attributes": {"tenant_id": "tenant-1"}},
            "input": {},
            "context": {},
        },
    )
    stored = await plans.get_plan("plan-preflight", tenant_id="tenant-1", user_id="user-1")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_not_available"
    assert registry.available_calls == 1
    assert invoker.calls == 0
    assert runs.runs == {}
    assert stored == original


async def test_route_and_invoke_consumes_the_route_candidate_set_once(
    non_lifespan_test_client,
) -> None:
    definitions = MemoryAgentDefinitionRepository()
    await definitions.upsert(
        AgentDefinition.model_validate(
            {
                "agent_id": "operator-agent",
                "name": "Operator Agent",
                "description": "Only operators may invoke this Agent.",
                "type": "mock",
                "access_policy": {"allow_roles": ["operator"]},
                "invocation": {"type": "mock", "config": {}},
            }
        )
    )
    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    registry = _CountingRegistry(settings, repository=definitions)
    await registry.load()
    router = RouterService(
        settings=settings,
        registry=registry,
        llm_client=_FixedTargetLLM("operator-agent"),
    )
    invoker = _RecordingInvoker()
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    invocation = InvocationService(
        registry=registry,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        invokers=invokers,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_router_service] = lambda: router
    app.dependency_overrides[get_invocation_service] = lambda: invocation

    response = non_lifespan_test_client(app).post(
        "/api/v1/route-and-invoke",
        headers=_headers(roles=["operator"]),
        json={
            "request_id": "route-invoke-1",
            "session_id": "session-1",
            "user": {"id": "user-1", "attributes": {"tenant_id": "tenant-1"}},
            "input": {"text": "use the operator Agent"},
        },
    )

    assert response.status_code == 200
    assert response.json()["result"]["status"] == "completed"
    assert registry.available_calls == 1
    assert invoker.calls == 1


async def test_each_plan_request_forms_and_reuses_one_fresh_candidate_set(
    non_lifespan_test_client,
) -> None:
    definitions = MemoryAgentDefinitionRepository()
    await definitions.upsert(
        AgentDefinition.model_validate(
            {
                "agent_id": "operator-agent",
                "name": "Operator Agent",
                "description": "Only operators may invoke this Agent.",
                "type": "mock",
                "access_policy": {"allow_roles": ["operator"]},
                "invocation": {"type": "mock", "config": {}},
            }
        )
    )
    registry = _CountingRegistry(
        Settings(storage_backend="memory", registry_backend="database"),
        repository=definitions,
    )
    await registry.load()
    plans = PlanService(MemoryPlanRepository())
    for plan_id in ("plan-confirm", "plan-confirm-execute", "plan-resume"):
        await plans.save_plan(
            Plan(
                plan_id=plan_id,
                session_id="session-1",
                user_id="user-1",
                tenant_id="tenant-1",
                status="pending",
                steps=[
                    {
                        "step_id": "step-1",
                        "agent_id": "operator-agent",
                        "description": "run",
                    }
                ],
            )
        )
    invoker = _RecordingInvoker()
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    executor = PlanExecutor(
        plan_service=plans,
        registry=registry,
        invocation_service=InvocationService(
            registry=registry,
            run_repository=MemoryRunRepository(),
            result_repository=MemoryResultRepository(),
            invokers=invokers,
            plan_service=plans,
        ),
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_plan_service] = lambda: plans
    app.dependency_overrides[get_plan_executor] = lambda: executor
    client = non_lifespan_test_client(app)
    user = {"id": "user-1", "attributes": {"tenant_id": "tenant-1"}}

    confirmed = client.post(
        "/api/v1/plans/plan-confirm/actions",
        headers=_headers(roles=["operator"]),
        json={"action": "confirm", "user": user},
    )
    confirmed_and_executed = client.post(
        "/api/v1/plans/plan-confirm-execute/confirm-and-execute",
        headers=_headers(roles=["operator"]),
        json={"user": user, "input": {}, "context": {}},
    )
    resumed = client.post(
        "/api/v1/plans/plan-resume/resume",
        headers=_headers(roles=["operator"]),
        json={"user": user, "input": {}, "context": {}},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "running"
    assert confirmed_and_executed.status_code == 200
    assert confirmed_and_executed.json()["plan"]["status"] == "completed"
    assert resumed.status_code == 200
    assert resumed.json()["plan"]["status"] == "completed"
    assert registry.available_calls == 3
    assert invoker.calls == 2


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_run_repository_owner_query_is_consistent(
    backend: str, tmp_path, managed_database
) -> None:
    if backend == "memory":
        runs = MemoryRunRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'owned-run.db'}",
        )
        await managed_database.initialize_schema(settings)
        session_factory = await managed_database.session_factory(settings)
        runs = DatabaseRunRepository(session_factory)
    await runs.add_run(
        AgentRun(
            run_id="run-contract",
            session_id="session-1",
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
        )
    )

    assert await runs.get_owned_run("run-contract", tenant_id="tenant-1", user_id="user-1")
    assert await runs.get_owned_run("run-contract", tenant_id="tenant-1", user_id="user-2") is None
    assert await runs.get_owned_run("run-contract", tenant_id="tenant-2", user_id="user-1") is None


def _headers(
    *,
    subject: str = "user-1",
    tenant: str = "tenant-1",
    roles: list[str] | None = None,
) -> dict[str, str]:
    return native_principal_headers(
        secret=_PRINCIPAL_SECRET,
        subject=subject,
        tenant=tenant,
        roles=roles,
    )


class _CapturingPlanExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *_args, **_kwargs) -> PlanExecutionResponse:
        self.calls += 1
        raise AssertionError("conflicting owner must be rejected before Plan execution")


class _RecordingInvoker:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, definition, invocation) -> AgentInvocationResult:
        self.calls += 1
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status="completed",
            message="done",
            output={},
        )


class _CountingRegistry(AgentRegistryService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.available_calls = 0

    async def available_definitions(self, user):
        self.available_calls += 1
        return await super().available_definitions(user)


class _FixedTargetLLM:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request.request_id or "request-1",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id=self.agent_id,
                confidence=1,
                reason="test",
                message="routing",
            ),
            context=RouteContext(
                relation="new_task",
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates],
            ),
        )
