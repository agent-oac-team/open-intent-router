from dataclasses import dataclass, field
from hashlib import sha256

import pytest

from app.core.config import Settings
from app.core.errors import (
    DirectInvocationUnsupportedError,
    InvocationBindingUnavailableError,
)
from app.repositories.database import DatabaseResultRepository, DatabaseRunRepository
from app.repositories.execution_traces import MemoryExecutionTraceRepository
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.execution_traces import ExecutionTraceQuery
from app.schemas.invocation import AgentInvocation, AgentInvocationResult, InvokeRequest
from app.services.binding_resolution import BindingResolver
from app.services.execution_trace_service import ExecutionTraceService
from app.services.invocation_service import InvocationService
from app.services.memory_formation import formation_turn_id
from app.services.registry_snapshot import (
    InvocationBindingRequirement,
    RegistrySnapshotBuilder,
    RegistrySnapshotRuntime,
)


class _NoRegistryReads:
    async def available_definitions(self, _user):  # pragma: no cover - must never be called
        raise AssertionError("Direct v2 Invoke must consume the selected Registry Snapshot")


@dataclass
class _V2Adapter:
    output: dict[str, object] = field(default_factory=lambda: {"status": "completed"})
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
            run_id="adapter-controlled-run-id",
            agent_id="adapter-controlled-agent-id",
            status="completed",
            output=self.output,
        )


class _SynchronousV2Adapter(_V2Adapter):
    def invoke_v2(
        self,
        definition: AgentDefinitionV2,
        requirement: InvocationBindingRequirement,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        self.calls.append((definition, requirement, invocation))
        return AgentInvocationResult(
            run_id="adapter-controlled-run-id",
            agent_id="adapter-controlled-agent-id",
            status="completed",
            output=self.output,
        )


class _WrongSignatureV2Adapter(_V2Adapter):
    async def invoke_v2(self, definition: AgentDefinitionV2) -> AgentInvocationResult:
        return AgentInvocationResult(
            run_id="adapter-controlled-run-id",
            agent_id=definition.agent_id,
            status="completed",
            output=self.output,
        )


class _DualProtocolAdapter(_V2Adapter):
    """A transition adapter whose Runtime method must not win by fallback."""

    async def execute(self, _binding: object, _envelope: object) -> object:
        raise AssertionError("Snapshot declared the legacy v2 execution protocol")


class _SwappingV2Adapter(_V2Adapter):
    """Exposes a compatible method once, then a different one on later lookup."""

    lookup_count = 0

    @property
    def invoke_v2(self):
        self.lookup_count += 1
        return self._accepted_invoke_v2 if self.lookup_count == 1 else self._changed_invoke_v2

    async def _accepted_invoke_v2(
        self,
        definition: AgentDefinitionV2,
        requirement: InvocationBindingRequirement,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        self.calls.append((definition, requirement, invocation))
        return AgentInvocationResult(
            run_id="adapter-controlled-run-id",
            agent_id="adapter-controlled-agent-id",
            status="completed",
            output=self.output,
        )

    def _changed_invoke_v2(
        self,
        _definition: AgentDefinitionV2,
        _requirement: InvocationBindingRequirement,
        _invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        return AgentInvocationResult(
            run_id="adapter-controlled-run-id",
            agent_id="adapter-controlled-agent-id",
            status="completed",
            output=self.output,
        )


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


def _descriptor(
    adapter: _V2Adapter,
    *,
    key: str = "test_adapter",
    contract_version: str = "adapter-contract-v1",
    implementation_version: str = "adapter-implementation-v2",
    v2_invocation: bool = True,
    invocation_runtime: bool = False,
) -> RuntimeAdapterDescriptor:
    return RuntimeAdapterDescriptor(
        key=key,
        contract_version=contract_version,
        implementation_version=implementation_version,
        config_schema={
            "type": "object",
            "required": ["function"],
            "properties": {"function": {"const": "execute"}},
            "additionalProperties": False,
        },
        capability=RuntimeAdapterCapability(
            invocation=True,
            v2_invocation=v2_invocation,
            invocation_runtime=invocation_runtime,
        ),
        factory=lambda _context: adapter,
        health_check=_healthy,
        lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
    )


async def _catalog(
    adapter: _V2Adapter,
    *,
    adapter_key: str = "test_adapter",
    contract_version: str = "adapter-contract-v1",
    implementation_version: str = "adapter-implementation-v2",
    v2_invocation: bool = True,
    invocation_runtime: bool = False,
) -> RuntimeCatalog:
    return await RuntimeCatalog.activate(
        [
            _descriptor(
                adapter,
                key=adapter_key,
                contract_version=contract_version,
                implementation_version=implementation_version,
                v2_invocation=v2_invocation,
                invocation_runtime=invocation_runtime,
            )
        ],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )


def _invocation_definition(**updates: object) -> AgentDefinitionV2:
    payload: dict[str, object] = {
        "schema_version": "oir-agent-v2",
        "agent_id": "bound-agent",
        "name": "Bound Agent",
        "description": "Executes a trusted binding.",
        "revision": 9,
        "access_policy": {"allow_roles": ["operator"]},
        "output_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
        "handling": {
            "kind": "invocation",
            "adapter_key": "test_adapter",
            "config": {"function": "execute"},
        },
    }
    payload.update(updates)
    return AgentDefinitionV2.model_validate(payload)


def _service(
    *,
    catalog: RuntimeCatalog,
    snapshot_runtime: RegistrySnapshotRuntime,
    runs,
    results,
    traces: ExecutionTraceService | None = None,
) -> InvocationService:
    return InvocationService(
        registry=_NoRegistryReads(),
        run_repository=runs,
        result_repository=results,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
        execution_traces=traces,
    )


def _request() -> InvokeRequest:
    return InvokeRequest(
        request_id="direct-request",
        session_id="direct-session",
        agent_id="bound-agent",
        user={
            "id": "operator-1",
            "roles": ["operator"],
            "attributes": {"tenant_id": "tenant-1"},
        },
        input={"text": "execute"},
    )


def _binding_version_fingerprint(value: str) -> str:
    return (
        "oir-binding-version-sha256-"
        + sha256(f"oir-binding-version-v1:{value}".encode()).hexdigest()
    )


async def test_direct_v2_invocation_resolves_before_run_and_persists_safe_binding() -> None:
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    trace_repository = MemoryExecutionTraceRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
        traces=ExecutionTraceService(trace_repository),
    )

    request = _request().model_copy(
        update={
            "context": {
                "caller_hint": "safe",
                "plan_id": "caller-controlled-plan",
                "_canonical_turn_managed": True,
                "_canonical_response_text": "must-not-replay",
            }
        }
    )
    first = await service.invoke(request)
    second = await service.invoke(request)

    assert first.status == second.status == "completed"
    assert first.run_id != second.run_id
    assert len(adapter.calls) == 2
    assert adapter.calls[0][0].revision == 9
    assert adapter.calls[0][1].adapter_key == "test_adapter"
    assert adapter.calls[0][2].run_id == first.run_id
    assert adapter.calls[0][2].context == {"caller_hint": "safe"}
    run = await runs.get_run(first.run_id)
    assert run is not None
    assert run.agent_revision == 9
    assert run.handling_kind == "invocation"
    assert run.binding_snapshot is not None
    assert run.binding_snapshot.model_dump() == {
        "schema_version": "oir-binding-v1",
        "kind": "invocation",
        "adapter_key": "test_adapter",
        "adapter_contract_version": _binding_version_fingerprint("adapter-contract-v1"),
        "adapter_implementation_version": _binding_version_fingerprint("adapter-implementation-v2"),
        "connector_ref": None,
        "connector_revision": None,
    }
    assert "function" not in run.binding_snapshot.model_dump_json()
    assert "endpoint" not in run.binding_snapshot.model_dump_json()
    assert "secret" not in run.binding_snapshot.model_dump_json()
    assert len(results.results) == 2

    trace_turn_id = formation_turn_id(
        tenant_id="tenant-1",
        user_id="operator-1",
        session_id="direct-session",
        request_id="direct-request",
        run_id=first.run_id,
    )
    trace = await trace_repository.list_events(
        ExecutionTraceQuery(
            tenant_id="tenant-1",
            user_id="operator-1",
            session_id="direct-session",
            turn_id=trace_turn_id,
        )
    )
    assert len(trace) == 1
    assert trace[0].facts == {
        "agent_id": "bound-agent",
        "invoker_type": "test_adapter",
        "delegated": False,
        "agent_revision": 9,
        "handling_kind": "invocation",
        "binding_schema_version": "oir-binding-v1",
        "adapter_contract_version": _binding_version_fingerprint("adapter-contract-v1"),
        "adapter_implementation_version": _binding_version_fingerprint("adapter-implementation-v2"),
    }
    assert "connector_ref" not in trace[0].facts

    await catalog.aclose()


@pytest.mark.parametrize(
    "handling",
    [
        {
            "kind": "external_execution",
            "executor_ref": "host_executor",
            "params": {"task": "execute"},
        },
        {
            "kind": "ui_handoff",
            "route": "/agents/continue",
            "params": {"tab": "continue"},
        },
    ],
)
async def test_direct_v2_invocation_rejects_non_invocation_handling_without_side_effects(
    handling: dict[str, object],
) -> None:
    adapter = _V2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(catalog, supported_executor_refs={"host_executor"})
    )
    snapshot_runtime.load([_invocation_definition(handling=handling)], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    with pytest.raises(DirectInvocationUnsupportedError) as exc_info:
        await service.invoke(_request())

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"handling_kind": handling["kind"]}
    assert runs.runs == {}
    assert results.results == []
    assert adapter.calls == []

    await catalog.aclose()


async def test_direct_v2_invocation_rejects_isolated_binding_before_run() -> None:
    adapter = _V2Adapter()
    catalog = await _catalog(adapter, v2_invocation=False)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    with pytest.raises(InvocationBindingUnavailableError) as exc_info:
        await service.invoke(_request())

    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {"reason_code": "invocation_adapter_incompatible"}
    assert runs.runs == {}
    assert results.results == []
    assert adapter.calls == []

    await catalog.aclose()


async def test_direct_v2_invocation_rejects_missing_adapter_before_run() -> None:
    adapter = _V2Adapter()
    catalog = await _catalog(adapter, adapter_key="other_adapter")
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    with pytest.raises(InvocationBindingUnavailableError) as exc_info:
        await service.invoke(_request())

    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {"reason_code": "invocation_adapter_missing"}
    assert runs.runs == {}
    assert results.results == []
    assert adapter.calls == []

    await catalog.aclose()


async def test_direct_v2_invocation_rejects_synchronous_adapter_before_run() -> None:
    adapter = _SynchronousV2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    with pytest.raises(InvocationBindingUnavailableError) as exc_info:
        await service.invoke(_request())

    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {"reason_code": "invocation_adapter_incompatible"}
    assert runs.runs == {}
    assert results.results == []
    assert adapter.calls == []

    await catalog.aclose()


async def test_direct_v2_invocation_rejects_wrong_adapter_signature_before_run() -> None:
    adapter = _WrongSignatureV2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    with pytest.raises(InvocationBindingUnavailableError) as exc_info:
        await service.invoke(_request())

    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {"reason_code": "invocation_adapter_incompatible"}
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


async def test_direct_v2_invocation_uses_the_preflight_validated_adapter_callable() -> None:
    adapter = _SwappingV2Adapter()
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    result = await service.invoke(_request())

    assert result.status == "completed"
    assert adapter.lookup_count == 1
    assert len(adapter.calls) == 1
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_direct_invocation_uses_the_protocol_frozen_by_the_snapshot() -> None:
    adapter = _DualProtocolAdapter()
    catalog = await _catalog(adapter, invocation_runtime=False)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    selection = snapshot_runtime.preflight_for_user(
        "bound-agent",
        _request().user,
    )
    assert selection is not None
    assert selection.binding_requirement.execution_protocol == "legacy_v2"
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    result = await service.invoke(_request())

    assert result.status == "completed"
    assert len(adapter.calls) == 1
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_legacy_v2_binding_with_a_connector_fails_before_run_acceptance() -> None:
    adapter = _DualProtocolAdapter()
    catalog = await _catalog(adapter, invocation_runtime=False)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            _invocation_definition(
                handling={
                    "kind": "invocation",
                    "adapter_key": "test_adapter",
                    "connector_ref": "tenant_connector",
                    "config": {"function": "execute"},
                }
            )
        ],
        source="test",
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    with pytest.raises(InvocationBindingUnavailableError) as exc_info:
        await service.invoke(_request())

    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {"reason_code": "connector_adapter_incompatible"}
    assert adapter.calls == []
    assert runs.runs == {}
    assert results.results == []

    await catalog.aclose()


@pytest.mark.parametrize(
    "descriptor_version",
    [
        "https://api.internal",
        "api.internal",
        "127.0.0.1",
        "api1",
        "db-1",
        "password1",
        "api-v1",
        "db-v1",
        "password-v1",
        "AKIA" + ("A" * 16) + "-v1",
    ],
)
async def test_direct_v2_invocation_projects_descriptor_versions_without_raw_persistence(
    descriptor_version: str,
) -> None:
    adapter = _V2Adapter()
    catalog = await _catalog(
        adapter,
        contract_version=descriptor_version,
        implementation_version=descriptor_version,
    )
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    trace_repository = MemoryExecutionTraceRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
        traces=ExecutionTraceService(trace_repository),
    )

    result = await service.invoke(_request())
    run = await runs.get_run(result.run_id)

    assert result.status == "completed"
    assert run is not None
    assert run.binding_snapshot is not None
    expected_version = _binding_version_fingerprint(descriptor_version)
    assert run.binding_snapshot.adapter_contract_version == expected_version
    assert run.binding_snapshot.adapter_implementation_version == expected_version
    assert descriptor_version not in run.binding_snapshot.model_dump_json()
    assert len(results.results) == len(adapter.calls) == 1

    trace_turn_id = formation_turn_id(
        tenant_id="tenant-1",
        user_id="operator-1",
        session_id="direct-session",
        request_id="direct-request",
        run_id=result.run_id,
    )
    trace = await trace_repository.list_events(
        ExecutionTraceQuery(
            tenant_id="tenant-1",
            user_id="operator-1",
            session_id="direct-session",
            turn_id=trace_turn_id,
        )
    )
    assert len(trace) == 1
    assert trace[0].facts["adapter_contract_version"] == expected_version
    assert trace[0].facts["adapter_implementation_version"] == expected_version
    assert descriptor_version not in trace[0].model_dump_json()

    await catalog.aclose()


async def test_accepted_v2_invocation_output_validation_still_records_a_terminal_run() -> None:
    adapter = _V2Adapter(output={"unexpected": True})
    catalog = await _catalog(adapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=results,
    )

    result = await service.invoke(_request())

    assert result.status == "invalid_output"
    assert result.error is not None
    assert result.error.code == "invalid_output"
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_direct_v2_invocation_persists_binding_facts_in_database(
    tmp_path, managed_database
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'binding.db'}"
    settings = Settings(storage_backend="database", database_url=database_url)
    await managed_database.initialize_schema(settings)
    adapter = _V2Adapter()
    catalog = await RuntimeCatalog.activate(
        [_descriptor(adapter)],
        RuntimeAdapterContext(settings=settings),
        shutdown_timeout_seconds=1,
    )
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_invocation_definition()], source="test")
    factory = await managed_database.session_factory(settings)
    runs = DatabaseRunRepository(factory)
    service = _service(
        catalog=catalog,
        snapshot_runtime=snapshot_runtime,
        runs=runs,
        results=DatabaseResultRepository(factory),
    )

    result = await service.invoke(_request())
    stored = await runs.get_run(result.run_id)

    assert stored is not None
    assert stored.agent_revision == 9
    assert stored.handling_kind == "invocation"
    assert stored.binding_snapshot is not None
    assert stored.binding_snapshot.adapter_key == "test_adapter"

    await catalog.aclose()
