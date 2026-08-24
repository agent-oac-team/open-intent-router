"""Reusable black-box conformance harness for Runtime Adapters.

Future trusted Adapters add a case here instead of reimplementing Catalog
activation, Binding Resolution, Runtime identity projection, and disposal
checks in each feature test.
"""

from __future__ import annotations

from dataclasses import dataclass

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


async def assert_runtime_adapter_conforms(
    case: RuntimeAdapterConformanceCase,
) -> AgentInvocationResult:
    """Exercise an Adapter only through its deployment Runtime contract."""

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
            binding_resolver=BindingResolver(catalog),
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
