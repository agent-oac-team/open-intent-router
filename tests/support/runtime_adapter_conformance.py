"""Reusable black-box conformance harness for Runtime Adapters.

Future trusted Adapters add a case here instead of reimplementing Catalog
activation, Binding Resolution, Runtime identity projection, and disposal
checks in each feature test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable

from app.application import ConnectorResolverApplicationPort
from app.core.config import Settings
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.runtime.catalog import RuntimeAdapterContext, RuntimeAdapterDescriptor, RuntimeCatalog
from app.runtime.invocation import InvocationRuntime
from app.schemas.agents import AgentDefinitionV2
from app.schemas.invocation import AgentInvocationResult, InvokeRequest
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime


@dataclass(frozen=True, slots=True)
class RuntimeAdapterConformanceCase:
    descriptor: RuntimeAdapterDescriptor
    definition: AgentDefinitionV2
    request: InvokeRequest
    connector_resolver: ConnectorResolverApplicationPort | None = None
    assert_disposed: Callable[[object], object | Awaitable[object]] | None = None


async def assert_runtime_adapter_conforms(
    case: RuntimeAdapterConformanceCase,
) -> AgentInvocationResult:
    """Exercise an Adapter only through its deployment Runtime contract."""

    # Catalog activation is the deployment gate: descriptor schema, lifecycle,
    # health and the closed Adapter method surface must all pass before a
    # Definition can be bound. The rest of this helper deliberately goes
    # through Snapshot -> Binding Resolver -> Invocation Runtime so it cannot
    # accidentally revive a request-time construction path in tests.
    catalog = await RuntimeCatalog.activate(
        [case.descriptor],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    try:
        snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
        snapshot_runtime.load([case.definition], source="runtime-adapter-conformance")
        runs = MemoryRunRepository()
        results = MemoryResultRepository()
        invocation_service = InvocationService(
            registry=object(),
            run_repository=runs,
            result_repository=results,
            snapshot_runtime=snapshot_runtime,
            binding_resolver=BindingResolver(catalog, connector_resolver=case.connector_resolver),
            invocation_runtime=InvocationRuntime(),
        )
        result = await invocation_service.invoke(case.request)
        assert result.run_id in runs.runs
        assert result.agent_id == case.definition.agent_id
        assert result.status == "completed"
        assert len(runs.runs) == len(results.results) == 1
        return result
    finally:
        await catalog.aclose()
        if case.assert_disposed is not None:
            adapter = catalog.get(case.descriptor.key)
            observed = case.assert_disposed(adapter)
            if isawaitable(observed):
                await observed
