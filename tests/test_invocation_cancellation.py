import asyncio
from dataclasses import dataclass, field

import pytest

from app.core.config import Settings
from app.repositories.database import DatabaseResultRepository, DatabaseRunRepository
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
    RuntimeCatalogValidationError,
)
from app.runtime.invocation import (
    AdapterControlEnvelope,
    AgentCallEnvelope,
    RawInvocationCancellationOutcome,
    RawInvocationOutcome,
    RuntimeAdapterBinding,
)
from app.runtime.local_function import (
    LocalFunctionRuntimeRegistry,
    local_function_runtime_descriptor,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.invocation import InvokeRequest
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


@dataclass
class _BlockingControlAdapter:
    stopped: bool = True
    raises_on_cancel: bool = False
    malformed_cancel_outcome: bool = False
    wait_for_cancel_release: bool = False
    execute_started: asyncio.Event = field(default_factory=asyncio.Event)
    execute_release: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_started: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_release: asyncio.Event = field(default_factory=asyncio.Event)
    execute_calls: int = 0
    cancel_calls: int = 0
    execution_ids: list[str] = field(default_factory=list)
    controls: list[AdapterControlEnvelope] = field(default_factory=list)

    async def execute(
        self,
        _binding: RuntimeAdapterBinding,
        _connector: object,
        _envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.execute_calls += 1
        self.execution_ids.append(_envelope.execution_id)
        self.execute_started.set()
        await self.execute_release.wait()
        return RawInvocationOutcome(output={"summary": "completed"})

    async def cancel(
        self,
        _binding: RuntimeAdapterBinding,
        control: AdapterControlEnvelope,
    ) -> RawInvocationCancellationOutcome:
        self.cancel_calls += 1
        self.controls.append(control)
        self.cancel_started.set()
        if self.wait_for_cancel_release:
            await self.cancel_release.wait()
        if self.raises_on_cancel:
            raise RuntimeError("adapter control secret=must-not-leak")
        if self.malformed_cancel_outcome:
            return RawInvocationCancellationOutcome.model_construct(
                stopped="secret-control-outcome-must-not-leak"
            )
        if self.stopped:
            self.execute_release.set()
        return RawInvocationCancellationOutcome(stopped=self.stopped)


class _UndeclaredControlAdapter(_BlockingControlAdapter):
    @property
    def cancel(self):
        raise AssertionError("undeclared Adapter cancellation must not be inspected")


def _definition(
    *,
    adapter_key: str = "control_adapter",
    config: dict[str, object] | None = None,
) -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "control-agent",
            "name": "Control Agent",
            "description": "Exercises truthful Runtime cancellation.",
            "revision": 1,
            "access_policy": {"allow_roles": ["operator"]},
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            },
            "handling": {
                "kind": "invocation",
                "adapter_key": adapter_key,
                "config": config or {"operation": "wait"},
            },
        }
    )


def _request(*, user_id: str = "operator-1", tenant_id: str = "tenant-1") -> InvokeRequest:
    return InvokeRequest.model_validate(
        {
            "request_id": "control-request",
            "session_id": "control-session",
            "agent_id": "control-agent",
            "user": {
                "id": user_id,
                "roles": ["operator"],
                "attributes": {"tenant_id": tenant_id},
            },
            "input": {"text": "run controlled work"},
        }
    )


async def _service_for(
    adapter: object,
    *,
    cancellation: bool,
    runs: object | None = None,
    results: object | None = None,
) -> tuple[RuntimeCatalog, InvocationService, object, object]:
    descriptor = RuntimeAdapterDescriptor(
        key="control_adapter",
        contract_version="control-contract-v1",
        implementation_version="control-implementation-v1",
        config_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {"operation": {"const": "wait"}},
            "additionalProperties": False,
        },
        capability=RuntimeAdapterCapability(
            invocation=True,
            cancellation=cancellation,
        ),
        factory=lambda _context: adapter,
        health_check=_healthy,
        lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
    )
    catalog = await RuntimeCatalog.activate(
        [descriptor],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([_definition()], source="invocation-control-test")
    runs = runs or MemoryRunRepository()
    results = results or MemoryResultRepository()
    service = InvocationService(
        registry=object(),
        run_repository=runs,
        result_repository=results,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
    )
    return catalog, service, runs, results


async def _only_run_id(runs: MemoryRunRepository) -> str:
    for _ in range(100):
        if len(runs.runs) == 1:
            return next(iter(runs.runs))
        await asyncio.sleep(0.01)
    raise AssertionError("accepted Run was not persisted")


async def _await_terminal(runs: MemoryRunRepository, run_id: str):
    for _ in range(100):
        run = await runs.get_run(run_id)
        if run is not None and run.status != "running":
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("accepted Run did not converge")


async def test_undeclared_adapter_control_is_never_inspected_or_called() -> None:
    adapter = _UndeclaredControlAdapter()
    catalog, service, runs, _results = await _service_for(adapter, cancellation=False)
    task = asyncio.create_task(service.invoke(_request()))
    try:
        await adapter.execute_started.wait()
        run_id = await _only_run_id(runs)

        response = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")

        assert response is not None
        assert response.model_dump() == {
            "run_id": run_id,
            "run_status": "running",
            "control_state": "unsupported",
            "completion_certainty": "unknown",
            "reason_code": "control_unsupported",
        }
        adapter.execute_release.set()
        assert (await task).status == "completed"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await catalog.aclose()


async def test_declared_but_invalid_control_protocol_rejects_before_run_acceptance() -> None:
    adapter = _BlockingControlAdapter()
    # Hide the valid async method after construction. The descriptor attests to
    # cancellation, so the Catalog must reject the mismatch during lifespan
    # activation, before any request can construct a Binding or Run.
    adapter.cancel = object()  # type: ignore[method-assign]
    with pytest.raises(RuntimeCatalogValidationError, match="cancellation protocol"):
        await _service_for(adapter, cancellation=True)


async def test_pre_dispatch_cancel_skips_adapter_and_converges_cancelled() -> None:
    adapter = _BlockingControlAdapter()
    catalog, service, runs, results = await _service_for(adapter, cancellation=True)
    trace_started = asyncio.Event()
    trace_release = asyncio.Event()

    async def block_trace(**_kwargs: object) -> None:
        trace_started.set()
        await trace_release.wait()

    service._record_binding_trace = block_trace  # type: ignore[method-assign]
    task = asyncio.create_task(service.invoke(_request()))
    try:
        await trace_started.wait()
        run_id = await _only_run_id(runs)

        response = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")

        assert response is not None
        assert response.control_state == "stop_confirmed"
        assert response.completion_certainty == "certain"
        assert adapter.execute_calls == adapter.cancel_calls == 0
        trace_release.set()
        result = await task
        stored_run = await runs.get_run(run_id)
        assert result.status == "cancelled"
        assert stored_run is not None and stored_run.status == "cancelled"
        assert len(results.results) == 1 and results.results[0].status == "cancelled"
    finally:
        trace_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await catalog.aclose()


async def test_confirmed_stop_converges_once_to_cancelled_and_is_idempotent() -> None:
    adapter = _BlockingControlAdapter()
    catalog, service, runs, results = await _service_for(adapter, cancellation=True)
    task = asyncio.create_task(service.invoke(_request()))
    try:
        await adapter.execute_started.wait()
        run_id = await _only_run_id(runs)

        response = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")
        result = await task
        terminal = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")

        assert response is not None
        assert response.control_state == "stop_confirmed"
        assert response.completion_certainty == "unknown"
        assert result.status == "cancelled"
        assert adapter.cancel_calls == 1
        assert adapter.controls == [AdapterControlEnvelope(execution_id=run_id)]
        assert len(results.results) == 1 and results.results[0].status == "cancelled"
        assert terminal is not None
        assert terminal.control_state == "cancelled"
        assert terminal.run_status == "cancelled"
    finally:
        adapter.execute_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await catalog.aclose()


async def test_confirmed_stop_persists_one_database_cancelled_run_and_result(
    tmp_path,
    managed_database,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'invocation-control.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    adapter = _BlockingControlAdapter()
    runs = DatabaseRunRepository(factory)
    results = DatabaseResultRepository(factory)
    catalog, service, _runs, _results = await _service_for(
        adapter,
        cancellation=True,
        runs=runs,
        results=results,
    )
    task = asyncio.create_task(service.invoke(_request()))
    try:
        await adapter.execute_started.wait()
        run_id = adapter.execution_ids[0]

        response = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")
        result = await task
        stored_run = await runs.get_run(run_id)
        stored_results = await results.list_recent(
            "control-session",
            tenant_id="tenant-1",
            user_id="operator-1",
        )

        assert response is not None and response.control_state == "stop_confirmed"
        assert result.status == "cancelled"
        assert stored_run is not None and stored_run.status == "cancelled"
        assert len(stored_results) == 1 and stored_results[0].status == "cancelled"
    finally:
        adapter.execute_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await catalog.aclose()


@pytest.mark.parametrize(
    ("stopped", "raises_on_cancel", "malformed_cancel_outcome"),
    [
        (False, False, False),
        (True, True, False),
        (True, False, True),
    ],
)
async def test_unconfirmed_or_failing_control_never_marks_run_cancelled(
    stopped: bool,
    raises_on_cancel: bool,
    malformed_cancel_outcome: bool,
) -> None:
    adapter = _BlockingControlAdapter(
        stopped=stopped,
        raises_on_cancel=raises_on_cancel,
        malformed_cancel_outcome=malformed_cancel_outcome,
    )
    catalog, service, runs, results = await _service_for(adapter, cancellation=True)
    task = asyncio.create_task(service.invoke(_request()))
    try:
        await adapter.execute_started.wait()
        run_id = await _only_run_id(runs)

        response = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")

        assert response is not None
        assert response.control_state == "stop_unconfirmed"
        assert response.completion_certainty == "unknown"
        assert response.reason_code == "control_stop_unconfirmed"
        assert adapter.cancel_calls == 1
        assert "secret" not in response.model_dump_json()
        repeated = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")
        assert repeated is not None
        assert repeated.control_state == "stop_unconfirmed"
        assert adapter.cancel_calls == 1
        adapter.execute_release.set()
        assert (await task).status == "completed"
        assert (await runs.get_run(run_id)).status == "completed"
        assert adapter.execute_calls == 1
        assert len(results.results) == 1
    finally:
        adapter.execute_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await catalog.aclose()


async def test_concurrent_control_requests_share_one_adapter_stop_attempt() -> None:
    adapter = _BlockingControlAdapter(wait_for_cancel_release=True)
    catalog, service, runs, _results = await _service_for(adapter, cancellation=True)
    invoke_task = asyncio.create_task(service.invoke(_request()))
    try:
        await adapter.execute_started.wait()
        run_id = await _only_run_id(runs)
        first = asyncio.create_task(
            service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")
        )
        second = asyncio.create_task(
            service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")
        )
        await adapter.cancel_started.wait()
        assert adapter.cancel_calls == 1
        adapter.cancel_release.set()

        first_response, second_response = await asyncio.gather(first, second)
        assert first_response is not None and second_response is not None
        assert first_response.control_state == second_response.control_state == "stop_confirmed"
        assert adapter.cancel_calls == 1
        assert (await invoke_task).status == "cancelled"
    finally:
        adapter.cancel_release.set()
        adapter.execute_release.set()
        if not invoke_task.done():
            invoke_task.cancel()
            await asyncio.gather(invoke_task, return_exceptions=True)
        await catalog.aclose()


async def test_control_is_owner_isolated_and_terminal_request_never_reaches_adapter() -> None:
    adapter = _BlockingControlAdapter()
    catalog, service, runs, _results = await _service_for(adapter, cancellation=True)
    task = asyncio.create_task(service.invoke(_request()))
    try:
        await adapter.execute_started.wait()
        run_id = await _only_run_id(runs)

        assert await service.cancel_run(run_id, tenant_id="tenant-1", user_id="other-user") is None
        assert await service.cancel_run(run_id, tenant_id="tenant-2", user_id="operator-1") is None
        assert adapter.cancel_calls == 0
        adapter.execute_release.set()
        assert (await task).status == "completed"

        terminal = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")
        assert terminal is not None and terminal.control_state == "terminal"
        assert adapter.cancel_calls == 0
    finally:
        adapter.execute_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await catalog.aclose()


async def test_local_function_control_survives_client_disconnect_until_process_stop() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    registry = LocalFunctionRuntimeRegistry()

    async def wait_function(_envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        started.set()
        await release.wait()
        return RawInvocationOutcome(output={"summary": "should not complete"})

    registry.register("wait", wait_function)
    descriptor = local_function_runtime_descriptor(registry, key="local_control")
    catalog = await RuntimeCatalog.activate(
        [descriptor],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [_definition(adapter_key="local_control", config={"function": "wait"})],
        source="local-control-test",
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = InvocationService(
        registry=object(),
        run_repository=runs,
        result_repository=results,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
    )
    task = asyncio.create_task(service.invoke(_request()))
    try:
        await started.wait()
        run_id = await _only_run_id(runs)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        response = await service.cancel_run(run_id, tenant_id="tenant-1", user_id="operator-1")
        terminal = await _await_terminal(runs, run_id)

        assert response is not None
        assert response.control_state == "stop_confirmed"
        assert terminal.status == "cancelled"
        assert len(results.results) == 1 and results.results[0].status == "cancelled"
    finally:
        release.set()
        await service.invocation_runtime.stop()
        await catalog.aclose()
