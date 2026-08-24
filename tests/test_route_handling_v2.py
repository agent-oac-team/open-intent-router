from dataclasses import dataclass, field

import pytest

from app.core.config import Settings
from app.core.errors import AgentUnavailableError, RoutingError
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.invocation import AgentInvocation, AgentInvocationResult
from app.schemas.plans import NextAction
from app.schemas.routing import (
    InvocationPreview,
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.registry_snapshot import (
    InvocationBindingRequirement,
    RegistrySnapshotBuilder,
    RegistrySnapshotRuntime,
)
from app.services.router_service import RouterService


class _NoRegistryReads:
    def __init__(self) -> None:
        self.calls = 0

    async def available_definitions(self, _user):
        self.calls += 1
        raise AssertionError("Route v2 must consume the request Snapshot, not the Registry")


@dataclass
class _V2Adapter:
    calls: list[tuple[AgentDefinitionV2, InvocationBindingRequirement, AgentInvocation]] = field(
        default_factory=list
    )

    async def invoke_v2(
        self,
        definition: AgentDefinitionV2,
        requirement: InvocationBindingRequirement,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        self.calls.append((definition, requirement, invocation))
        return AgentInvocationResult(
            run_id="adapter-run-id",
            agent_id="adapter-agent-id",
            status="completed",
            output={"status": "completed"},
        )


class _TargetLLM:
    def __init__(self, target_agent_id: str, *, provisional_mode: str = "deferred") -> None:
        self.target_agent_id = target_agent_id
        self.provisional_mode = provisional_mode
        self.payload: LLMRouteInput | None = None

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        self.payload = payload
        return RouteResponse(
            request_id=payload.request.request_id or "route-v2-request",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id=self.target_agent_id,
                confidence=0.9,
                reason="Targeted test route.",
                message="Continue with the selected Agent.",
            ),
            context=RouteContext(candidate_agent_ids=[self.target_agent_id]),
            invocation=InvocationPreview(
                mode=self.provisional_mode,
                agent_id=self.target_agent_id,
                input={"host_supplied": "must-not-control-handling"},
                metadata={"host_handling": "must-not-control-handling"},
            ),
        )


class _ReplyWithUntrustedActionLLM:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request.request_id or "route-v2-request",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="reply",
                confidence=0.9,
                reason="No Agent should be selected.",
                message="A safe textual reply.",
            ),
            context=RouteContext(
                candidate_agent_ids=[item.agent_id for item in payload.candidates]
            ),
            next_action=NextAction(
                type="open_ui",
                route="https://untrusted.example/redirect",
                params={"redirect": "untrusted"},
            ),
        )


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


async def _catalog(adapter: _V2Adapter) -> RuntimeCatalog:
    return await RuntimeCatalog.activate(
        [
            RuntimeAdapterDescriptor(
                key="v2_adapter",
                contract_version="route-contract-v1",
                implementation_version="route-implementation-v1",
                config_schema={
                    "type": "object",
                    "required": ["function"],
                    "properties": {"function": {"const": "execute"}},
                    "additionalProperties": False,
                },
                capability=RuntimeAdapterCapability(invocation=True, v2_invocation=True),
                factory=lambda _context: adapter,
                health_check=_healthy,
                lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
            )
        ],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )


def _definition(
    agent_id: str,
    *,
    handling: dict[str, object],
    allow_roles: list[str] | None = None,
    allow_tenants: list[str] | None = None,
    revision: int = 4,
) -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": agent_id,
            "name": agent_id.replace("-", " ").title(),
            "description": "A route handling test Agent.",
            "revision": revision,
            "access_policy": {
                "allow_roles": allow_roles or ["operator"],
                "allow_tenants": allow_tenants or [],
            },
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            "handling": handling,
        }
    )


def _request(
    *,
    frontend_context: dict[str, object] | None = None,
    user_id: str = "route-v2-user",
    roles: list[str] | None = None,
    tenant_id: str = "route-v2-tenant",
    session_id: str = "route-v2-session",
    request_id: str | None = "route-v2-request",
) -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": request_id,
            "session_id": session_id,
            "user": {
                "id": user_id,
                "roles": roles or ["operator"],
                "attributes": {"tenant_id": tenant_id},
            },
            "input": {"text": "execute this routed request"},
            "frontend_context": frontend_context or {},
        }
    )


def _router(
    *,
    settings: Settings,
    registry: _NoRegistryReads,
    snapshot_runtime: RegistrySnapshotRuntime,
    llm: _TargetLLM,
) -> RouterService:
    return RouterService(
        settings=settings,
        registry=registry,
        llm_client=llm,
        snapshot_runtime=snapshot_runtime,
    )


def _invocation_service(
    *,
    catalog: RuntimeCatalog,
    registry: _NoRegistryReads,
    snapshot_runtime: RegistrySnapshotRuntime,
    runs: MemoryRunRepository,
    results: MemoryResultRepository,
) -> InvocationService:
    return InvocationService(
        registry=registry,
        run_repository=runs,
        result_repository=results,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
    )


async def test_route_v2_invocation_reuses_exact_snapshot_binding_without_registry_reread() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "invoke-agent",
                handling={
                    "kind": "invocation",
                    "adapter_key": "v2_adapter",
                    "config": {"function": "execute"},
                },
            )
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    llm = _TargetLLM("invoke-agent", provisional_mode="ui_handoff")
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=llm,
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = _invocation_service(
        catalog=catalog,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )
    request = _request(frontend_context={"handling_kind": "ui_handoff"})

    route = await router.route(request)

    assert route.invocation is not None
    assert route.invocation.mode == "invoke"
    assert route.invocation.input == {"text": request.input.text}
    assert route.selected_binding("invoke-agent") is not None
    assert "v2_adapter" not in route.model_dump_json()
    assert registry.calls == 0
    assert llm.payload is not None
    model_candidate = llm.payload.candidates[0].model_dump()
    assert not {"adapter_key", "connector_ref", "executor_ref"} & model_candidate.keys()
    assert runs.runs == {}
    assert results.results == []

    result = await invocation.invoke_from_route(request, route)

    assert result is not None and result.status == "completed"
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0].revision == 4
    assert adapter.calls[0][1].connector_ref is None
    assert "host_handling" not in adapter.calls[0][2].context
    assert len(runs.runs) == len(results.results) == 1
    assert registry.calls == 0

    await catalog.aclose()


async def test_route_v2_ui_handoff_returns_only_safe_host_action_without_execution() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "handoff-agent",
                handling={
                    "kind": "ui_handoff",
                    "route": "/workspace/continue",
                    "params": {"tab": "continue"},
                },
            )
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=_TargetLLM("handoff-agent", provisional_mode="invoke"),
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = _invocation_service(
        catalog=catalog,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )
    request = _request(frontend_context={"handling_kind": "invocation"})

    route = await router.route(request)

    assert route.invocation is None
    assert route.next_action is not None
    assert route.next_action.type == "open_ui"
    assert route.next_action.route == "/workspace/continue"
    assert route.next_action.params == {"tab": "continue"}
    assert route.next_action.metadata == {"handling_kind": "ui_handoff"}
    assert await invocation.invoke_from_route(request, route) is None
    assert runs.runs == {}
    assert results.results == []
    assert adapter.calls == []
    assert registry.calls == 0

    await catalog.aclose()


async def test_route_v2_fails_closed_when_model_targets_an_inaccessible_snapshot_entry() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "visible-agent",
                handling={
                    "kind": "invocation",
                    "adapter_key": "v2_adapter",
                    "config": {"function": "execute"},
                },
            ),
            _definition(
                "admin-agent",
                handling={
                    "kind": "ui_handoff",
                    "route": "/admin/only",
                    "params": {},
                },
                allow_roles=["admin"],
            ),
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    llm = _TargetLLM("admin-agent")
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=llm,
    )

    with pytest.raises(RoutingError, match="outside the candidate set"):
        await router.route(_request())

    assert llm.payload is not None
    assert [candidate.agent_id for candidate in llm.payload.candidates] == ["visible-agent"]
    assert registry.calls == 0
    assert adapter.calls == []

    await catalog.aclose()


async def test_route_v2_rejects_cross_principal_snapshot_replay_without_execution() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "invoke-agent",
                handling={
                    "kind": "invocation",
                    "adapter_key": "v2_adapter",
                    "config": {"function": "execute"},
                },
                allow_tenants=["route-v2-tenant"],
            )
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=_TargetLLM("invoke-agent"),
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = _invocation_service(
        catalog=catalog,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )
    route = await router.route(_request())

    for replay in (
        _request(roles=["viewer"]),
        _request(tenant_id="other-tenant"),
    ):
        with pytest.raises(AgentUnavailableError, match="not available"):
            await invocation.invoke_from_route(replay, route)

    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []
    assert registry.calls == 0

    await catalog.aclose()


async def test_route_v2_rejects_same_principal_request_replay_without_execution() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "invoke-agent",
                handling={
                    "kind": "invocation",
                    "adapter_key": "v2_adapter",
                    "config": {"function": "execute"},
                },
            )
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=_TargetLLM("invoke-agent"),
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = _invocation_service(
        catalog=catalog,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )
    route = await router.route(_request(request_id=None))

    for replay in (
        _request(request_id="a-different-request"),
        _request(request_id=None),
    ):
        with pytest.raises(AgentUnavailableError, match="not available"):
            await invocation.invoke_from_route(replay, route)

    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []
    assert registry.calls == 0

    await catalog.aclose()


async def test_route_v2_rejects_tampered_invocation_target_without_execution() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "first-agent",
                handling={
                    "kind": "invocation",
                    "adapter_key": "v2_adapter",
                    "config": {"function": "execute"},
                },
            ),
            _definition(
                "second-agent",
                handling={
                    "kind": "invocation",
                    "adapter_key": "v2_adapter",
                    "config": {"function": "execute"},
                },
            ),
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=_TargetLLM("first-agent"),
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = _invocation_service(
        catalog=catalog,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )
    request = _request()
    route = await router.route(request)
    assert route.invocation is not None
    route.invocation = route.invocation.model_copy(update={"agent_id": "second-agent"})

    with pytest.raises(AgentUnavailableError, match="not available"):
        await invocation.invoke_from_route(request, route)

    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []
    assert registry.calls == 0

    await catalog.aclose()


async def test_route_v2_discards_untrusted_host_action_without_selected_agent() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "handoff-agent",
                handling={
                    "kind": "ui_handoff",
                    "route": "/workspace/continue",
                    "params": {"tab": "continue"},
                },
            )
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=_ReplyWithUntrustedActionLLM(),
    )

    route = await router.route(_request())

    assert route.decision.action == "reply"
    assert route.invocation is None
    assert route.next_action is None
    assert "untrusted.example" not in route.model_dump_json()
    assert registry.calls == 0

    await catalog.aclose()


async def test_route_v2_rejects_rebinding_a_ui_handoff_as_an_invocation() -> None:
    settings = Settings(storage_backend="memory")
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _definition(
                "handoff-agent",
                handling={
                    "kind": "ui_handoff",
                    "route": "/workspace/continue",
                    "params": {"tab": "continue"},
                },
            ),
            _definition(
                "invoke-agent",
                handling={
                    "kind": "invocation",
                    "adapter_key": "v2_adapter",
                    "config": {"function": "execute"},
                },
            ),
        ],
        source="route-test",
    )
    registry = _NoRegistryReads()
    router = _router(
        settings=settings,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        llm=_TargetLLM("handoff-agent"),
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = _invocation_service(
        catalog=catalog,
        registry=registry,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )
    request = _request()
    route = await router.route(request)
    assert route.invocation is None
    route.decision = RouteDecision(
        status="ok",
        action="open_agent",
        target_agent_id="invoke-agent",
        reason="tampered",
    )
    route.invocation = InvocationPreview(
        mode="invoke", agent_id="invoke-agent", input={"text": "x"}
    )

    with pytest.raises(RuntimeError, match="immutable"):
        route.bind_routed_execution(request)
    with pytest.raises(AgentUnavailableError, match="not available"):
        await invocation.invoke_from_route(request, route)

    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []
    assert registry.calls == 0

    await catalog.aclose()
