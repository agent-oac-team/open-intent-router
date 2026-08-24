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
from app.runtime.invocation import AgentCallEnvelope, RawInvocationOutcome, RuntimeAdapterBinding
from app.schemas.common import UserContext
from app.schemas.plans import Plan
from app.services.binding_resolution import BindingResolver
from app.services.plan_bindings import freeze_plan_step_binding
from app.services.registry_service import AgentRegistryService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime


async def _no_op(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


@dataclass
class V2TestAdapter:
    """A deterministic Runtime Adapter for orchestration-focused tests."""

    error: Exception | None = None
    invocations: list[AgentCallEnvelope] = field(default_factory=list)

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.invocations.append(envelope)
        if self.error is not None:
            raise self.error
        # Test behavior is selected exclusively from the already-safe Runtime
        # binding.  In particular, this helper never receives a Definition or
        # legacy Invocation object merely to synthesize a response shape.
        function = binding.config.get("function")
        if function == "invalid":
            return RawInvocationOutcome(output={"summary": 123})
        if function == "summarize":
            return RawInvocationOutcome(output={"summary": "test summary"})
        if function == "create_task":
            return RawInvocationOutcome(
                output={
                    "task_id": "task-test",
                    "title": str(envelope.input.get("title") or "test task"),
                }
            )
        return RawInvocationOutcome(message="test adapter completed.")


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
        capability=RuntimeAdapterCapability(invocation=True),
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
