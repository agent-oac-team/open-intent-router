"""Small v2-only Runtime Catalog helpers for orchestration tests.

These helpers deliberately model an activated Adapter and immutable Snapshot.
They avoid reviving the former type-based Invoker path merely to support tests
that are concerned with Router, Plan, Context, or ownership behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings
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
from app.schemas.plans import Plan
from app.services.binding_resolution import BindingResolver
from app.services.plan_bindings import freeze_plan_step_binding
from app.services.registry_service import AgentRegistryService
from app.services.registry_snapshot import (
    InvocationBindingRequirement,
    RegistrySnapshotBuilder,
    RegistrySnapshotRuntime,
)


async def _no_op(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


@dataclass
class V2TestAdapter:
    """A deterministic Adapter for tests that do not themselves test adapters."""

    error: Exception | None = None
    status: str = "completed"
    invocations: list[AgentInvocation] = field(default_factory=list)

    async def invoke_v2(
        self,
        definition: AgentDefinitionV2,
        requirement: InvocationBindingRequirement,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        self.invocations.append(invocation)
        if self.error is not None:
            raise self.error
        properties = definition.output_schema.properties
        if requirement.config.get("function") == "invalid":
            output: dict[str, object] = {"summary": 123}
        elif "summary" in properties:
            output: dict[str, object] = {"summary": "test summary"}
        elif "task_id" in properties:
            output = {
                "task_id": "task-test",
                "title": str(invocation.input.get("title") or "test task"),
            }
        else:
            output = {
                key: "test value"
                for key, schema in properties.items()
                if isinstance(schema, dict) and schema.get("type") == "string"
            }
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status=self.status,
            output=output,
        )


async def activate_v2_test_catalog(
    *,
    adapter_key: str = "mock",
    adapter: V2TestAdapter | None = None,
) -> RuntimeCatalog:
    adapter = adapter or V2TestAdapter()
    descriptor = RuntimeAdapterDescriptor(
        key=adapter_key,
        contract_version="test-v2-adapter-contract",
        implementation_version="test-v2-adapter-implementation",
        config_schema={"type": "object"},
        capability=RuntimeAdapterCapability(invocation=True, v2_invocation=True),
        factory=lambda _context: adapter,
        health_check=_healthy,
        lifecycle=RuntimeAdapterLifecycle(activate=_no_op, dispose=_no_op),
    )
    return await RuntimeCatalog.activate(
        [descriptor],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )


async def attach_v2_runtime(
    registry: AgentRegistryService,
    *,
    adapter: V2TestAdapter | None = None,
    external_executor: object | None = None,
) -> RuntimeCatalog:
    """Publish a v2 test Catalog/Snapshot alongside an already-loaded Registry."""

    catalog = await activate_v2_test_catalog(adapter=adapter)
    snapshot_runtime = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(catalog, external_executor=external_executor)
    )
    snapshot_runtime.load(
        registry.state.agents,
        source=f"test-{registry.state.active_source or 'registry'}",
    )
    registry.snapshot_runtime = snapshot_runtime
    registry.binding_resolver = BindingResolver(catalog)
    return catalog


class SnapshotAwareRegistryService(AgentRegistryService):
    """Test Registry that refreshes its published v2 Snapshot on explicit loads."""

    def __init__(self, *args, runtime_catalog: RuntimeCatalog, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(runtime_catalog))
        self.binding_resolver = BindingResolver(runtime_catalog)

    async def load(self):
        state = await super().load()
        self.snapshot_runtime.load(state.agents, source=f"fixture-{state.active_source}")
        return state


def refresh_attached_v2_snapshot(registry: AgentRegistryService) -> None:
    """Refresh a test-only published Snapshot after the test commits Registry data."""

    snapshot_runtime = getattr(registry, "snapshot_runtime", None)
    if not isinstance(snapshot_runtime, RegistrySnapshotRuntime):
        raise AssertionError("v2 test Snapshot was not attached to the Registry")
    snapshot_runtime.load(
        registry.state.agents,
        source=f"test-{registry.state.active_source or 'registry'}",
    )


def freeze_plan_bindings(
    plan: Plan,
    registry: AgentRegistryService,
    *,
    user: UserContext,
) -> Plan:
    """Return a v2 Plan whose unfinished Steps freeze this test Snapshot."""

    snapshot_runtime = getattr(registry, "snapshot_runtime", None)
    if (
        not isinstance(snapshot_runtime, RegistrySnapshotRuntime)
        or snapshot_runtime.snapshot is None
    ):
        raise AssertionError("v2 test Snapshot was not attached to the Registry")
    steps = []
    for step in plan.steps:
        if step.status in {"completed", "failed", "cancelled"}:
            steps.append(step)
            continue
        selection = snapshot_runtime.snapshot.select_for_user(step.agent_id, user)
        if selection is None:
            raise AssertionError(f"test Definition is unavailable: {step.agent_id}")
        steps.append(freeze_plan_step_binding(step, selection))
    return plan.model_copy(update={"steps": steps})
