from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.api.plans import confirm_and_execute_plan
from app.core.config import Settings
from app.core.errors import (
    AgentUnavailableError,
    InvocationBindingUnavailableError,
    PlanBindingUnavailableError,
)
from app.repositories.database import DatabasePlanRepository
from app.repositories.memory import (
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.turns import MemoryTurnRepository
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import UserContext
from app.schemas.invocation import AgentInvocation, AgentInvocationResult
from app.schemas.plans import Plan, PlanExecutionRequest, PlanStep
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
from app.schemas.security import NativePrincipal
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.plan_bindings import freeze_plan_step_binding
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from app.services.registry_snapshot import (
    InvocationBindingRequirement,
    RegistrySnapshotBuilder,
    RegistrySnapshotRuntime,
)
from app.services.router_service import RouterService
from app.services.turn_service import TurnService


class _NoRegistryReads:
    async def available_definitions(self, _user):  # pragma: no cover - assertion path
        raise AssertionError("v2 Plan execution must use the current Registry Snapshot")


class _ProtocolBrokenAdapter:
    """Advertises v2 capability but lacks the required async invocation protocol."""


class _ReloadingPlanService:
    """Reload the Snapshot after execute() has captured its Candidate Set."""

    def __init__(
        self,
        delegate: PlanService,
        snapshot_runtime: RegistrySnapshotRuntime,
        replacement: AgentDefinitionV2,
    ) -> None:
        self.delegate = delegate
        self.snapshot_runtime = snapshot_runtime
        self.replacement = replacement
        self.get_calls = 0

    async def get_plan(self, *args, **kwargs):
        self.get_calls += 1
        if self.get_calls == 2:
            self.snapshot_runtime.load([self.replacement], source="reload-during-plan-execute")
        return await self.delegate.get_plan(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class _ReloadingConfirmPlanService:
    """Reload only after confirm-and-execute has formed its Candidate Set."""

    def __init__(
        self,
        delegate: PlanService,
        snapshot_runtime: RegistrySnapshotRuntime,
        replacement: AgentDefinitionV2,
    ) -> None:
        self.delegate = delegate
        self.snapshot_runtime = snapshot_runtime
        self.replacement = replacement
        self.reloaded = False

    async def confirm(self, *args, **kwargs):
        if not self.reloaded:
            self.snapshot_runtime.load([self.replacement], source="reload-during-confirm")
            self.reloaded = True
        return await self.delegate.confirm(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


@dataclass
class _Adapter:
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
            run_id="adapter-run",
            agent_id="adapter-agent",
            status="completed",
            output={"status": "completed"},
        )


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


async def _catalog(
    adapter: object,
    *,
    v2_invocation: bool = True,
    contract_version: str = "plan-contract-v1",
    implementation_version: str = "plan-implementation-v1",
) -> RuntimeCatalog:
    return await RuntimeCatalog.activate(
        [
            RuntimeAdapterDescriptor(
                key="plan_adapter",
                contract_version=contract_version,
                implementation_version=implementation_version,
                config_schema={
                    "type": "object",
                    "required": ["function"],
                    "properties": {"function": {"const": "execute"}},
                    "additionalProperties": False,
                },
                capability=RuntimeAdapterCapability(invocation=True, v2_invocation=v2_invocation),
                factory=lambda _context: adapter,
                health_check=_healthy,
                lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
            )
        ],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )


def _definition(
    *,
    agent_id: str = "plan-agent",
    revision: int = 9,
    allow_roles: list[str] | None = None,
    connector_ref: str = "plan_connector",
    handling: dict[str, object] | None = None,
) -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": agent_id,
            "name": "Plan Agent",
            "description": "Executes a delayed Plan Step.",
            "revision": revision,
            "access_policy": {"allow_roles": allow_roles or ["operator"]},
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
            "handling": handling
            or {
                "kind": "invocation",
                "adapter_key": "plan_adapter",
                "connector_ref": connector_ref,
                "config": {"function": "execute"},
            },
        }
    )


def _user() -> UserContext:
    return UserContext(
        id="plan-user",
        roles=["operator"],
        attributes={"tenant_id": "plan-tenant"},
    )


async def _plan(
    *,
    plan_service: PlanService,
    snapshot_runtime: RegistrySnapshotRuntime,
    status: str = "pending",
) -> Plan:
    selection = snapshot_runtime.snapshot.select_for_user("plan-agent", _user())
    assert selection is not None
    plan = Plan(
        plan_id="bound-plan",
        user_id="plan-user",
        tenant_id="plan-tenant",
        session_id="plan-session",
        status=status,
        steps=[
            freeze_plan_step_binding(
                PlanStep(
                    step_id="bound-step",
                    agent_id="plan-agent",
                    description="Use the frozen v2 binding.",
                ),
                selection,
            )
        ],
    )
    return await plan_service.save_plan(plan)


async def _executor(
    *,
    catalog: RuntimeCatalog,
    snapshot_runtime: RegistrySnapshotRuntime,
    plans: MemoryPlanRepository,
    runs: MemoryRunRepository,
    results: MemoryResultRepository,
) -> PlanExecutor:
    plan_service = PlanService(plans)
    invocation = InvocationService(
        registry=_NoRegistryReads(),
        run_repository=runs,
        result_repository=results,
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
    )
    return PlanExecutor(
        plan_service=plan_service,
        registry=_NoRegistryReads(),
        invocation_service=invocation,
    )


async def test_v2_plan_execution_revalidates_and_resolves_before_creating_a_run() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(
        plan_service=plan_service, snapshot_runtime=snapshot_runtime, status="running"
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )

    response = await executor.execute(
        plan.plan_id,
        user=_user(),
        input_values={"text": "run the frozen step"},
    )

    assert response.plan.status == "completed"
    assert [item["step_id"] for item in response.results] == ["bound-step"]
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0].revision == 9
    assert adapter.calls[0][1].connector_ref == "plan_connector"
    assert len(runs.runs) == len(results.results) == 1
    run = next(iter(runs.runs.values()))
    assert run.agent_revision == 9
    assert run.binding_snapshot is not None

    await catalog.aclose()


async def test_v2_plan_execution_keeps_one_captured_snapshot_for_all_steps() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition(revision=9)], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        status="running",
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = InvocationService(
        registry=_NoRegistryReads(),
        run_repository=runs,
        result_repository=results,
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
    )
    executor = PlanExecutor(
        plan_service=_ReloadingPlanService(
            plan_service,
            snapshot_runtime,
            _definition(revision=10),
        ),
        registry=_NoRegistryReads(),
        invocation_service=invocation,
    )

    response = await executor.execute(
        plan.plan_id,
        user=_user(),
        input_values={"text": "hold the original candidate set"},
    )

    assert response.plan.status == "completed"
    assert adapter.calls[0][0].revision == 9
    assert (
        snapshot_runtime.snapshot.select_for_user("plan-agent", _user()).definition.revision == 10
    )

    await catalog.aclose()


async def test_v2_route_and_execute_reuses_the_route_snapshot_after_reload() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition(revision=9)], source="route")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        status="running",
    )
    route_selection = snapshot_runtime.snapshot.select_for_user("plan-agent", _user())
    assert route_selection is not None
    # Model the boundary between RouterService.route() and PlanExecutor.execute():
    # a reload must not change the accepted request's Candidate Set.
    snapshot_runtime.load([_definition(revision=10)], source="reloaded-before-execute")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )

    response = await executor.execute(
        plan.plan_id,
        user=_user(),
        input_values={"text": "preserve the Route selection"},
        selected_bindings={"plan-agent": route_selection},
    )

    assert response.plan.status == "completed"
    assert adapter.calls[0][0].revision == 9

    await catalog.aclose()


async def test_confirm_and_execute_reuses_its_preflight_snapshot_after_reload() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition(revision=9)], source="confirm-preflight")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    invocation = InvocationService(
        registry=_NoRegistryReads(),
        run_repository=runs,
        result_repository=results,
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
    )
    executor = PlanExecutor(
        plan_service=_ReloadingConfirmPlanService(
            plan_service,
            snapshot_runtime,
            _definition(revision=10),
        ),
        registry=_NoRegistryReads(),
        invocation_service=invocation,
    )

    response = await confirm_and_execute_plan(
        plan.plan_id,
        PlanExecutionRequest(
            user=_user(),
            input={"text": "keep the preflight Candidate Set"},
        ),
        principal=NativePrincipal(
            claims_version="oir-principal-v1",
            subject="plan-user",
            tenant="plan-tenant",
            roles=["operator"],
        ),
        plan_service=executor.plan_service,
        executor=executor,
    )

    assert response.plan.status == "completed"
    assert adapter.calls[0][0].revision == 9
    assert (
        snapshot_runtime.snapshot.select_for_user("plan-agent", _user()).definition.revision == 10
    )

    await catalog.aclose()


async def test_completed_v2_plan_returns_without_a_live_snapshot() -> None:
    """Terminal state is durable; only a resumed Step needs revalidation."""

    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    initial = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    completed = await plan_service.save_plan(
        initial.model_copy(
            update={
                "status": "completed",
                "current_step_id": None,
                "steps": [initial.steps[0].model_copy(update={"status": "completed"})],
            }
        )
    )
    executor = PlanExecutor(
        plan_service=plan_service,
        registry=_NoRegistryReads(),
        invocation_service=SimpleNamespace(),
    )

    response = await executor.execute(
        completed.plan_id,
        user=_user(),
        input_values={"text": "read terminal state"},
    )

    assert response.plan.status == "completed"
    assert response.results == []

    await catalog.aclose()


async def test_failed_v2_plan_with_pending_successor_returns_without_a_live_snapshot() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    initial = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    selection = snapshot_runtime.snapshot.select_for_user("plan-agent", _user())
    assert selection is not None
    failed = await plan_service.save_plan(
        initial.model_copy(
            update={
                "status": "failed",
                "current_step_id": None,
                "steps": [
                    initial.steps[0].model_copy(update={"status": "failed"}),
                    freeze_plan_step_binding(
                        PlanStep(
                            step_id="pending-successor",
                            agent_id="plan-agent",
                            description="Must not require a Snapshot after terminal failure.",
                            depends_on=[initial.steps[0].step_id],
                        ),
                        selection,
                    ),
                ],
            }
        )
    )
    executor = PlanExecutor(
        plan_service=plan_service,
        registry=_NoRegistryReads(),
        invocation_service=SimpleNamespace(),
    )

    response = await executor.execute(
        failed.plan_id,
        user=_user(),
        input_values={"text": "read failed terminal Plan"},
    )

    assert response.plan.status == "failed"
    assert response.results == []
    assert await executor.preflight(failed.plan_id, user=_user()) == {}

    await catalog.aclose()


async def test_failed_controlled_plan_returns_without_a_live_candidate_set() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    initial = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    selection = snapshot_runtime.snapshot.select_for_user("plan-agent", _user())
    assert selection is not None
    failed = await plan_service.save_plan(
        initial.model_copy(
            update={
                "status": "failed",
                "current_step_id": None,
                "steps": [
                    initial.steps[0].model_copy(update={"status": "failed"}),
                    freeze_plan_step_binding(
                        PlanStep(
                            step_id="pending-successor",
                            agent_id="plan-agent",
                            description="Do not resume a terminal Plan.",
                            depends_on=[initial.steps[0].step_id],
                        ),
                        selection,
                    ),
                ],
            }
        )
    )
    router = RouterService(
        settings=Settings(storage_backend="memory"),
        registry=_NoRegistryReads(),
        llm_client=_PlanLLM(),
        plan_service=plan_service,
    )

    response = await router.route(
        RouteRequest.model_validate(
            {
                "request_id": "failed-plan-control",
                "session_id": "plan-session",
                "source": "plan_control",
                "plan_id": failed.plan_id,
                "user": _user().model_dump(mode="json"),
                "input": {"text": "report the terminal outcome"},
            }
        )
    )

    assert response.decision.action == "reply"
    assert response.plan is not None and response.plan.status == "failed"
    assert response.next_action is None

    await catalog.aclose()


@pytest.mark.parametrize(
    ("replacement", "reason_code"),
    [
        (_definition(revision=10), "plan_binding_revision_incompatible"),
        (
            _definition(connector_ref="replacement_connector"),
            "plan_binding_requirement_incompatible",
        ),
    ],
)
async def test_v2_plan_rejects_changed_definition_or_binding_before_execution(
    replacement: AgentDefinitionV2,
    reason_code: str,
) -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )
    snapshot_runtime.load([replacement], source="replacement")

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await executor.execute(
            plan.plan_id,
            user=_user(),
            input_values={"text": "must not start"},
        )

    assert exc_info.value.details == {"reason_code": reason_code}
    stored = await plan_service.get_plan(plan.plan_id, tenant_id="plan-tenant", user_id="plan-user")
    assert stored is not None
    assert stored.status == "pending"
    assert stored.steps[0].status == "pending"
    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


async def test_v2_plan_rechecks_current_principal_before_execution() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )
    snapshot_runtime.load([_definition(allow_roles=["administrator"])], source="permission-change")

    with pytest.raises(AgentUnavailableError):
        await executor.execute(
            plan.plan_id,
            user=_user(),
            input_values={"text": "must not start"},
        )

    stored = await plan_service.get_plan(plan.plan_id, tenant_id="plan-tenant", user_id="plan-user")
    assert stored is not None and stored.status == "pending"
    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


async def test_v2_plan_resume_rechecks_the_current_binding_before_mutating_blocked_step() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    created = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    blocked = await plan_service.save_plan(
        created.model_copy(
            update={
                "status": "blocked",
                "steps": [created.steps[0].model_copy(update={"status": "blocked"})],
            }
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )
    snapshot_runtime.load([_definition(revision=10)], source="replacement")

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await executor.execute(
            blocked.plan_id,
            user=_user(),
            input_values={"text": "resume only if still compatible"},
        )

    assert exc_info.value.details == {"reason_code": "plan_binding_revision_incompatible"}
    stored = await plan_service.get_plan(
        blocked.plan_id, tenant_id="plan-tenant", user_id="plan-user"
    )
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.steps[0].status == "blocked"
    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


async def test_v2_plan_rejects_an_incompatible_restarted_adapter_before_execution() -> None:
    initial_adapter = _Adapter()
    initial_catalog = await _catalog(initial_adapter)
    initial_snapshot = RegistrySnapshotRuntime(RegistrySnapshotBuilder(initial_catalog))
    initial_snapshot.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=initial_snapshot)

    restarted_adapter = _Adapter()
    restarted_catalog = await _catalog(restarted_adapter, v2_invocation=False)
    restarted_snapshot = RegistrySnapshotRuntime(RegistrySnapshotBuilder(restarted_catalog))
    restarted_snapshot.load([_definition()], source="restarted")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=restarted_catalog,
        snapshot_runtime=restarted_snapshot,
        plans=plans,
        runs=runs,
        results=results,
    )

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await executor.execute(
            plan.plan_id,
            user=_user(),
            input_values={"text": "must not start after restart"},
        )

    assert exc_info.value.details == {"reason_code": "invocation_adapter_incompatible"}
    stored = await plan_service.get_plan(plan.plan_id, tenant_id="plan-tenant", user_id="plan-user")
    assert stored is not None and stored.status == "pending"
    assert initial_adapter.calls == []
    assert restarted_adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await initial_catalog.aclose()
    await restarted_catalog.aclose()


async def test_v2_plan_resolves_adapter_protocol_before_marking_the_step_running() -> None:
    broken_adapter = _ProtocolBrokenAdapter()
    catalog = await _catalog(broken_adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )

    with pytest.raises(InvocationBindingUnavailableError) as exc_info:
        await executor.execute(
            plan.plan_id,
            user=_user(),
            input_values={"text": "must fail before Plan state changes"},
        )

    assert exc_info.value.details == {"reason_code": "invocation_adapter_incompatible"}
    stored = await plan_service.get_plan(plan.plan_id, tenant_id="plan-tenant", user_id="plan-user")
    assert stored is not None and stored.status == "pending"
    assert stored.steps[0].status == "pending"
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


async def test_v2_plan_resolves_a_compatible_restarted_adapter_version() -> None:
    initial_adapter = _Adapter()
    initial_catalog = await _catalog(initial_adapter)
    initial_snapshot = RegistrySnapshotRuntime(RegistrySnapshotBuilder(initial_catalog))
    initial_snapshot.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=initial_snapshot)

    restarted_adapter = _Adapter()
    restarted_catalog = await _catalog(
        restarted_adapter,
        contract_version="plan-contract-v2",
        implementation_version="plan-implementation-v2",
    )
    restarted_snapshot = RegistrySnapshotRuntime(RegistrySnapshotBuilder(restarted_catalog))
    restarted_snapshot.load([_definition()], source="restarted")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=restarted_catalog,
        snapshot_runtime=restarted_snapshot,
        plans=plans,
        runs=runs,
        results=results,
    )

    response = await executor.execute(
        plan.plan_id,
        user=_user(),
        input_values={"text": "use the replacement adapter"},
    )

    assert response.plan.status == "completed"
    assert initial_adapter.calls == []
    assert len(restarted_adapter.calls) == 1
    assert len(runs.runs) == 1

    await initial_catalog.aclose()
    await restarted_catalog.aclose()


async def test_v2_plan_never_falls_back_to_legacy_registry_when_snapshot_is_unavailable() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    initial_snapshot = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    initial_snapshot.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=initial_snapshot)
    unavailable_snapshot = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=unavailable_snapshot,
        plans=plans,
        runs=runs,
        results=results,
    )

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await executor.execute(
            plan.plan_id,
            user=_user(),
            input_values={"text": "must not use a legacy fallback"},
        )

    assert exc_info.value.details == {"reason_code": "plan_snapshot_unavailable"}
    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


async def test_native_executor_rejects_an_unfrozen_legacy_plan_before_execution() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="v2-only")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan(
            plan_id="unfrozen-plan",
            user_id="plan-user",
            tenant_id="plan-tenant",
            session_id="plan-session",
            status="running",
            steps=[
                PlanStep(
                    step_id="unfrozen-step",
                    agent_id="plan-agent",
                    description="A pre-v2 Step without a frozen Binding.",
                )
            ],
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await executor.execute(
            plan.plan_id,
            user=_user(),
            input_values={"text": "do not infer a legacy invoker"},
        )

    assert exc_info.value.details == {
        "reason_code": "legacy_plan_step_unsupported",
        "agent_ids": ["plan-agent"],
    }
    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


@pytest.mark.parametrize(
    ("handling", "action_type", "handling_kind"),
    [
        (
            {
                "kind": "ui_handoff",
                "route": "/plans/continue",
                "params": {"tab": "continue"},
            },
            "open_ui",
            "ui_handoff",
        ),
        (
            {
                "kind": "external_execution",
                "executor_ref": "host_executor",
                "params": {"task": "continue"},
            },
            "wait_for_agent_event",
            "external_execution",
        ),
    ],
)
async def test_v2_plan_revalidates_before_returning_host_handoff_action(
    handling: dict[str, object],
    action_type: str,
    handling_kind: str,
) -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(catalog, supported_executor_refs={"host_executor"})
    )
    payload = _definition().model_dump(mode="json")
    payload["handling"] = handling
    snapshot_runtime.load([AgentDefinitionV2.model_validate(payload)], source="handoff")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(
        plan_service=plan_service, snapshot_runtime=snapshot_runtime, status="running"
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )

    response = await executor.execute(
        plan.plan_id,
        user=_user(),
        input_values={"text": "continue"},
    )

    assert response.plan.status == "blocked"
    assert response.next_action is not None
    assert response.next_action.type == action_type
    assert response.next_action.metadata == {"handling_kind": handling_kind}
    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


async def test_v2_plan_rejects_an_external_executor_removed_after_planning() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    definition = _definition(
        handling={
            "kind": "external_execution",
            "executor_ref": "host_executor",
            "params": {"task": "continue"},
        }
    )
    initial_snapshot = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(catalog, supported_executor_refs={"host_executor"})
    )
    initial_snapshot.load([definition], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=initial_snapshot)
    unavailable_snapshot = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(catalog, supported_executor_refs=set())
    )
    unavailable_snapshot.load([definition], source="executor-removed")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=unavailable_snapshot,
        plans=plans,
        runs=runs,
        results=results,
    )

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await executor.execute(
            plan.plan_id,
            user=_user(),
            input_values={"text": "do not create an external run"},
        )

    assert exc_info.value.details == {"reason_code": "external_executor_unsupported"}
    stored = await plan_service.get_plan(plan.plan_id, tenant_id="plan-tenant", user_id="plan-user")
    assert stored is not None and stored.status == "pending"
    assert adapter.calls == []
    assert runs.runs == {}

    await catalog.aclose()


async def test_v2_plan_revalidates_and_completes_normal_dependent_steps() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    selection = snapshot_runtime.snapshot.select_for_user("plan-agent", _user())
    assert selection is not None
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan(
            plan_id="two-step-bound-plan",
            user_id="plan-user",
            tenant_id="plan-tenant",
            session_id="plan-session",
            steps=[
                freeze_plan_step_binding(
                    PlanStep(
                        step_id="first-bound-step",
                        agent_id="plan-agent",
                        description="Run the first Step.",
                    ),
                    selection,
                ),
                freeze_plan_step_binding(
                    PlanStep(
                        step_id="second-bound-step",
                        agent_id="plan-agent",
                        description="Run after the first Step.",
                        depends_on=["first-bound-step"],
                    ),
                    selection,
                ),
            ],
        )
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    executor = await _executor(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        plans=plans,
        runs=runs,
        results=results,
    )

    response = await executor.execute(
        plan.plan_id,
        user=_user(),
        input_values={"text": "complete every step"},
    )

    assert response.plan.status == "completed"
    assert [step.status for step in response.plan.steps] == ["completed", "completed"]
    assert len(response.results) == 2
    assert len(adapter.calls) == 2
    assert len(runs.runs) == 2

    await catalog.aclose()


class _PlanLLM:
    async def route(self, payload) -> RouteResponse:
        agent_id = payload.candidates[0].agent_id
        return RouteResponse(
            request_id=payload.request.request_id or "bound-plan-route",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="show_plan",
                confidence=1.0,
                reason="Create a two-step Plan.",
                message="Plan created.",
            ),
            context=RouteContext(relation="multi_task", candidate_agent_ids=[agent_id]),
            execution_policy="require_confirmation",
            plan=Plan(
                plan_id="router-bound-plan",
                user_id="untrusted-user",
                tenant_id="untrusted-tenant",
                session_id=payload.request.session_id,
                steps=[
                    PlanStep(step_id="step-one", agent_id=agent_id, description="First"),
                    PlanStep(
                        step_id="step-two",
                        agent_id=agent_id,
                        description="Second",
                        depends_on=["step-one"],
                    ),
                ],
            ),
        )


class _ReloadingTurnService:
    def __init__(
        self,
        delegate: TurnService,
        snapshot_runtime: RegistrySnapshotRuntime,
        replacement: AgentDefinitionV2,
    ) -> None:
        self.delegate = delegate
        self.snapshot_runtime = snapshot_runtime
        self.replacement = replacement
        self.reloaded = False

    async def start_turn(self, **kwargs):
        if not self.reloaded:
            self.snapshot_runtime.load([self.replacement], source="reload-during-turn-start")
            self.reloaded = True
        return await self.delegate.start_turn(**kwargs)

    async def attach_activity(self, **kwargs):
        return await self.delegate.attach_activity(**kwargs)


async def test_controlled_v2_plan_keeps_one_captured_snapshot_across_turn_start() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition(revision=9)], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    created = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    running = await plan_service.save_plan(
        created.model_copy(update={"status": "running", "current_step_id": "bound-step"})
    )
    turns = MemoryTurnRepository()
    router = RouterService(
        settings=Settings(storage_backend="memory"),
        registry=_NoRegistryReads(),
        llm_client=_PlanLLM(),
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
        turn_service=_ReloadingTurnService(
            TurnService(turns),
            snapshot_runtime,
            _definition(revision=10),
        ),
    )

    response = await router.route(
        RouteRequest.model_validate(
            {
                "request_id": "snapshot-fence-control",
                "session_id": "plan-session",
                "source": "plan_control",
                "plan_id": running.plan_id,
                "user": _user().model_dump(mode="json"),
                "input": {"text": "continue"},
            }
        )
    )

    binding = response.selected_binding("plan-agent")
    assert binding is not None
    assert binding.definition.revision == 9
    assert (
        snapshot_runtime.snapshot.select_for_user("plan-agent", _user()).definition.revision == 10
    )

    await catalog.aclose()


async def test_controlled_native_route_rejects_unfrozen_step_before_creating_a_turn() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="v2-current-step")
    selection = snapshot_runtime.snapshot.select_for_user("plan-agent", _user())
    assert selection is not None
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan(
            plan_id="unfrozen-controlled-plan",
            user_id="plan-user",
            tenant_id="plan-tenant",
            session_id="plan-session",
            status="running",
            current_step_id="unfrozen-step",
            steps=[
                PlanStep(
                    step_id="unfrozen-step",
                    agent_id="plan-agent",
                    description="A pre-v2 controlled Step without a frozen Binding.",
                    status="running",
                ),
            ],
        )
    )
    turns = MemoryTurnRepository()
    router = RouterService(
        settings=Settings(storage_backend="memory"),
        registry=_NoRegistryReads(),
        llm_client=_PlanLLM(),
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
        turn_service=TurnService(turns),
    )

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await router.route(
            RouteRequest.model_validate(
                {
                    "request_id": "unfrozen-controlled-route",
                    "session_id": "plan-session",
                    "source": "plan_control",
                    "plan_id": plan.plan_id,
                    "user": _user().model_dump(mode="json"),
                    "input": {"text": "continue"},
                }
            )
        )

    assert turns.turns == {}
    assert exc_info.value.details == {
        "reason_code": "legacy_plan_step_unsupported",
        "agent_ids": ["plan-agent"],
    }

    await catalog.aclose()


async def test_router_persists_hidden_frozen_v2_plan_bindings() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="route")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    router = RouterService(
        settings=Settings(storage_backend="memory"),
        registry=_NoRegistryReads(),
        llm_client=_PlanLLM(),
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
    )

    response = await router.route(
        RouteRequest.model_validate(
            {
                "request_id": "bound-plan-route",
                "session_id": "plan-session",
                "user": _user().model_dump(mode="json"),
                "input": {"text": "make a plan"},
            }
        )
    )

    assert response.plan is not None
    assert all(step.agent_revision == 9 for step in response.plan.steps)
    assert all(step.binding_requirement is not None for step in response.plan.steps)
    assert "plan_adapter" not in response.model_dump_json()
    assert "plan_connector" not in response.model_dump_json()
    stored = await plan_service.get_plan(
        "router-bound-plan", tenant_id="plan-tenant", user_id="plan-user"
    )
    assert stored is not None
    assert all(step.agent_revision == 9 for step in stored.steps)
    assert all(step.binding_requirement is not None for step in stored.steps)

    await catalog.aclose()


async def test_plan_control_rejects_stale_v2_binding_before_host_execution_is_exposed() -> None:
    adapter = _Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    turns = MemoryTurnRepository()
    router = RouterService(
        settings=Settings(storage_backend="memory"),
        registry=_NoRegistryReads(),
        llm_client=_PlanLLM(),
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        turn_service=TurnService(turns),
    )
    initial = await router.route(
        RouteRequest.model_validate(
            {
                "request_id": "plan-control-create",
                "session_id": "plan-session",
                "user": _user().model_dump(mode="json"),
                "input": {"text": "make a plan"},
            }
        )
    )
    assert initial.plan is not None
    await plan_service.confirm(
        initial.plan.plan_id,
        tenant_id="plan-tenant",
        user_id="plan-user",
    )
    turn_count_before_rejection = len(turns.turns)
    snapshot_runtime.load([_definition(revision=10)], source="replacement")

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await router.route(
            RouteRequest.model_validate(
                {
                    "request_id": "plan-control-stale",
                    "session_id": "plan-session",
                    "source": "plan_control",
                    "plan_id": initial.plan.plan_id,
                    "user": _user().model_dump(mode="json"),
                    "input": {"text": "continue"},
                }
            )
        )

    assert exc_info.value.details == {"reason_code": "plan_binding_revision_incompatible"}
    stored = await plan_service.get_plan(
        initial.plan.plan_id,
        tenant_id="plan-tenant",
        user_id="plan-user",
    )
    assert stored is not None and stored.status == "running"
    assert adapter.calls == []
    assert len(turns.turns) == turn_count_before_rejection
    assert "plan-control-stale" not in turns.request_ids

    await catalog.aclose()


async def test_plan_control_rejects_broken_v2_adapter_before_creating_a_turn() -> None:
    catalog = await _catalog(_ProtocolBrokenAdapter())
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="initial")
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await _plan(plan_service=plan_service, snapshot_runtime=snapshot_runtime)
    running = await plan_service.save_plan(
        plan.model_copy(update={"status": "running", "current_step_id": "bound-step"})
    )
    turns = MemoryTurnRepository()
    router = RouterService(
        settings=Settings(storage_backend="memory"),
        registry=_NoRegistryReads(),
        llm_client=_PlanLLM(),
        plan_service=plan_service,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
        turn_service=TurnService(turns),
    )

    with pytest.raises(PlanBindingUnavailableError) as exc_info:
        await router.route(
            RouteRequest.model_validate(
                {
                    "request_id": "plan-control-broken-adapter",
                    "session_id": "plan-session",
                    "source": "plan_control",
                    "plan_id": running.plan_id,
                    "user": _user().model_dump(mode="json"),
                    "input": {"text": "continue"},
                }
            )
        )

    assert exc_info.value.details == {"reason_code": "invocation_adapter_incompatible"}
    assert turns.turns == {}
    stored = await plan_service.get_plan(
        running.plan_id,
        tenant_id="plan-tenant",
        user_id="plan-user",
    )
    assert stored is not None and stored.status == "running"

    await catalog.aclose()


async def test_database_plan_repository_round_trips_hidden_v2_binding_requirement(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'plan-bindings.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabasePlanRepository(await managed_database.session_factory(settings))
    plan_service = PlanService(repository)
    definition = _definition()
    plan = Plan(
        plan_id="database-bound-plan",
        user_id="plan-user",
        tenant_id="plan-tenant",
        steps=[
            PlanStep(
                step_id="database-bound-step",
                agent_id="plan-agent",
                description="Persist only the declarative binding.",
                agent_revision=definition.revision,
                binding_requirement=definition.handling,
            )
        ],
    )

    created = await plan_service.save_plan(plan)
    claim = await plan_service.claim_step(
        created.plan_id,
        "database-bound-step",
        tenant_id="plan-tenant",
        user_id="plan-user",
        lease_seconds=60,
    )
    assert claim is not None
    claimed, claim_id = claim
    completed = await plan_service.finish_claimed_step(
        created.plan_id,
        "database-bound-step",
        tenant_id="plan-tenant",
        user_id="plan-user",
        claim_id=claim_id,
        status="completed",
    )
    assert completed is not None and completed.status == "completed"
    restored = await plan_service.get_plan(
        created.plan_id,
        tenant_id="plan-tenant",
        user_id="plan-user",
    )

    assert restored is not None
    assert claimed.steps[0].binding_requirement == definition.handling
    step = restored.steps[0]
    assert step.agent_revision == 9
    assert step.binding_requirement == definition.handling
    assert "binding_requirement" not in restored.model_dump(mode="json")
    plan_step_schema = Plan.model_json_schema()["$defs"]["PlanStep"]["properties"]
    assert "agent_revision" not in plan_step_schema
    assert "binding_requirement" not in plan_step_schema
