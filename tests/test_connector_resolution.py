"""Invocation Connector resolution is request-scoped and fails before Run acceptance."""

from __future__ import annotations

import asyncio
import gc
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.application import ConnectorResolutionRequest, ResolvedConnector
from app.core.config import Settings
from app.core.errors import (
    InvocationBindingUnavailableError,
    InvocationDeadlineExceededError,
    InvocationPreflightRejectedError,
)
from app.repositories.execution_traces import MemoryExecutionTraceRepository
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.runtime.catalog import (
    RuntimeAdapterCapability,
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeAdapterLifecycle,
    RuntimeCatalog,
)
from app.runtime.invocation import (
    AgentCallEnvelope,
    InvocationRuntime,
    InvocationRuntimePolicy,
    RawInvocationFailure,
    RawInvocationOutcome,
    RuntimeAdapterBinding,
)
from app.schemas.agents import AgentDefinitionV2
from app.schemas.execution_traces import ExecutionTraceQuery
from app.schemas.invocation import AgentInvocation, InvokeRequest
from app.services.binding_resolution import BindingResolver
from app.services.execution_trace_service import ExecutionTraceService
from app.services.invocation_service import InvocationService
from app.services.memory_formation import formation_turn_id
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


@dataclass
class _Connection:
    endpoint: str
    authorization: str
    closed: bool = False


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, duration: timedelta) -> None:
        self.now += duration


@dataclass
class _RecordingConnectorResolver:
    connector: ResolvedConnector | None
    failure: BaseException | None = None
    requests: list[ConnectorResolutionRequest] = field(default_factory=list)
    released: list[ResolvedConnector] = field(default_factory=list)

    async def resolve(self, request: ConnectorResolutionRequest) -> ResolvedConnector | None:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return self.connector

    async def release(self, connector: ResolvedConnector) -> None:
        self.released.append(connector)
        if isinstance(connector.connection, _Connection):
            connector.connection.closed = True


@dataclass
class _DeadlineAdvancingConnectorResolver(_RecordingConnectorResolver):
    clock: _Clock | None = None
    advance_by: timedelta = timedelta(0)

    async def resolve(self, request: ConnectorResolutionRequest) -> ResolvedConnector | None:
        self.requests.append(request)
        if self.clock is not None:
            self.clock.advance(self.advance_by)
        if self.failure is not None:
            raise self.failure
        return self.connector


@dataclass
class _BlockingReleaseDeadlineConnectorResolver(_DeadlineAdvancingConnectorResolver):
    """Makes post-deadline cleanup ownership observable without blocking 504."""

    release_started: asyncio.Event = field(default_factory=asyncio.Event)
    allow_release: asyncio.Event = field(default_factory=asyncio.Event)

    async def release(self, connector: ResolvedConnector) -> None:
        self.released.append(connector)
        self.release_started.set()
        await self.allow_release.wait()
        if isinstance(connector.connection, _Connection):
            connector.connection.closed = True


@dataclass
class _CancellationDefiantReleaseConnectorResolver(_RecordingConnectorResolver):
    """Models a deployment release hook that ignores shutdown cancellation."""

    release_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    allow_release: asyncio.Event = field(default_factory=asyncio.Event)

    async def release(self, connector: ResolvedConnector) -> None:
        self.released.append(connector)
        self.release_started.set()
        while not self.allow_release.is_set():
            try:
                await self.allow_release.wait()
            except asyncio.CancelledError:
                self.release_cancelled.set()
        if isinstance(connector.connection, _Connection):
            connector.connection.closed = True


@dataclass
class _DeadlineDefiantConnectorResolver(_RecordingConnectorResolver):
    """Returns a Connector only after suppressing the preflight timeout."""

    resolution_started: asyncio.Event = field(default_factory=asyncio.Event)
    deadline_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    allow_finish: asyncio.Event = field(default_factory=asyncio.Event)

    async def resolve(self, request: ConnectorResolutionRequest) -> ResolvedConnector | None:
        self.requests.append(request)
        self.resolution_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.deadline_cancelled.set()
            await self.allow_finish.wait()
        return self.connector


@dataclass
class _RecordingRuntimeAdapter:
    outcome: RawInvocationOutcome = field(
        default_factory=lambda: RawInvocationOutcome(output={"summary": "completed"})
    )
    sleep_seconds: float = 0.0
    failure: BaseException | None = None
    calls: list[tuple[RuntimeAdapterBinding, ResolvedConnector | None, AgentCallEnvelope]] = field(
        default_factory=list
    )

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        connector: ResolvedConnector | None,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append((binding, connector, envelope))
        if self.failure is not None:
            raise self.failure
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        return self.outcome


@dataclass
class _BlockingConnectorPreparerAdapter(_RecordingRuntimeAdapter):
    """Stops in pre-acceptance preparation so cancellation cleanup is observable."""

    preparation_started: asyncio.Event = field(default_factory=asyncio.Event)
    allow_preparation: asyncio.Event = field(default_factory=asyncio.Event)

    async def prepare_connector(
        self,
        _binding: RuntimeAdapterBinding,
        connector: ResolvedConnector,
    ) -> ResolvedConnector:
        self.preparation_started.set()
        await self.allow_preparation.wait()
        return connector


@dataclass
class _DeadlineDefiantConnectorPreparerAdapter(_RecordingRuntimeAdapter):
    """Suppresses one deadline cancellation during pre-acceptance DNS work."""

    preparation_started: asyncio.Event = field(default_factory=asyncio.Event)
    deadline_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    allow_finish: asyncio.Event = field(default_factory=asyncio.Event)

    async def prepare_connector(
        self,
        _binding: RuntimeAdapterBinding,
        connector: ResolvedConnector,
    ) -> ResolvedConnector:
        self.preparation_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.deadline_cancelled.set()
            await self.allow_finish.wait()
        return connector


@dataclass
class _BlockingRuntimeAdapter(_RecordingRuntimeAdapter):
    """Makes accepted client-disconnect cleanup observable with a Connector."""

    execution_started: asyncio.Event = field(default_factory=asyncio.Event)
    allow_completion: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        connector: ResolvedConnector | None,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append((binding, connector, envelope))
        self.execution_started.set()
        await self.allow_completion.wait()
        return self.outcome


@dataclass
class _CancellationDefiantConnectorAdapter:
    """Suppresses one deadline cancellation, then cooperates at shutdown."""

    calls: list[tuple[RuntimeAdapterBinding, ResolvedConnector | None, AgentCallEnvelope]] = field(
        default_factory=list
    )
    deadline_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    cancellations: int = 0

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        connector: ResolvedConnector | None,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append((binding, connector, envelope))
        while True:
            try:
                # The Future is otherwise unreferenced.  InvocationRuntime's
                # late-task ownership is what keeps this adapter observable.
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancellations += 1
                if self.cancellations == 1:
                    self.deadline_cancelled.set()
                    continue
                self.shutdown_cancelled.set()
                return RawInvocationOutcome(output={"summary": "stopped"})


def _connector(
    *,
    tenant_id: str = "tenant-1",
    adapter_key: str = "connector_adapter",
    connector_ref: str = "tenant-copywriter",
    revision: str = "connector-r7",
    connection: object | None = None,
) -> ResolvedConnector:
    return ResolvedConnector(
        tenant_id=tenant_id,
        adapter_key=adapter_key,
        connector_ref=connector_ref,
        revision=revision,
        connection=connection
        if connection is not None
        else _Connection(
            endpoint="https://connector.invalid/private-operation",
            authorization="Bearer connector-test-secret-marker",
        ),
    )


def _definition(*, required_knowledge: bool = False) -> AgentDefinitionV2:
    knowledge: dict[str, object] = {"mode": "disabled"}
    if required_knowledge:
        knowledge = {"mode": "prefetch", "requirement": "required"}
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "connector-agent",
            "name": "Connector Agent",
            "description": "Executes through a deployment Connector.",
            "revision": 7,
            "access_policy": {"allow_roles": ["operator"]},
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
            "context": {"memory": {"mode": "disabled"}, "knowledge": knowledge},
            "handling": {
                "kind": "invocation",
                "adapter_key": "connector_adapter",
                "connector_ref": "tenant-copywriter",
                "config": {"operation": "write"},
            },
        }
    )


def _request() -> InvokeRequest:
    return InvokeRequest.model_validate(
        {
            "request_id": "connector-request",
            "session_id": "connector-session",
            "agent_id": "connector-agent",
            "user": {
                "id": "operator-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            "input": {
                "text": "write the approved output",
                # Untrusted input cannot overwrite the frozen Connector scope.
                "tenant_id": "tenant-2",
                "connector_ref": "attacker-selected-connector",
            },
        }
    )


async def _service(
    *,
    resolver: _RecordingConnectorResolver | None,
    adapter: _RecordingRuntimeAdapter,
    definition: AgentDefinitionV2 | None = None,
    traces: ExecutionTraceService | None = None,
    runtime: InvocationRuntime | None = None,
) -> tuple[RuntimeCatalog, InvocationService, MemoryRunRepository, MemoryResultRepository]:
    descriptor = RuntimeAdapterDescriptor(
        key="connector_adapter",
        contract_version="connector-contract-v1",
        implementation_version="connector-implementation-v1",
        config_schema={
            "type": "object",
            "required": ["operation"],
            "properties": {"operation": {"const": "write"}},
            "additionalProperties": False,
        },
        capability=RuntimeAdapterCapability(
            invocation=True,
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
    snapshot_runtime.load([definition or _definition()], source="connector-resolution-test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    return (
        catalog,
        InvocationService(
            registry=object(),
            run_repository=runs,
            result_repository=results,
            snapshot_runtime=snapshot_runtime,
            binding_resolver=BindingResolver(catalog, connector_resolver=resolver),
            execution_traces=traces,
            invocation_runtime=runtime,
        ),
        runs,
        results,
    )


async def test_connector_is_bound_to_the_trusted_request_and_never_enters_envelope_or_trace() -> (
    None
):
    private_connection = _Connection(
        endpoint="https://connector.invalid/private-operation",
        authorization="Bearer connector-test-secret-marker",
    )
    connector = _connector(connection=private_connection)
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _RecordingRuntimeAdapter()
    trace_repository = MemoryExecutionTraceRepository()
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        traces=ExecutionTraceService(trace_repository),
    )
    try:
        result = await service.invoke(_request())

        assert result.status == "completed"
        assert len(resolver.requests) == 1
        resolution_request = resolver.requests[0]
        assert resolution_request.tenant_id == "tenant-1"
        assert resolution_request.principal.id == "operator-1"
        assert resolution_request.adapter_key == "connector_adapter"
        assert resolution_request.connector_ref == "tenant-copywriter"
        assert len(adapter.calls) == 1
        binding, delivered_connector, envelope = adapter.calls[0]
        assert binding.adapter_key == "connector_adapter"
        assert dict(binding.config) == {"operation": "write"}
        assert delivered_connector is connector
        assert delivered_connector.connection is private_connection
        assert "connector-test-secret-marker" not in envelope.model_dump_json()
        assert "connector.invalid" not in envelope.model_dump_json()
        assert "tenant-copywriter" not in envelope.model_dump_json()

        run = await runs.get_run(result.run_id)
        assert run is not None and run.binding_snapshot is not None
        assert run.binding_snapshot.model_dump() == {
            "schema_version": "oir-binding-v1",
            "kind": "invocation",
            "adapter_key": "connector_adapter",
            "adapter_contract_version": run.binding_snapshot.adapter_contract_version,
            "adapter_implementation_version": run.binding_snapshot.adapter_implementation_version,
            "connector_ref": "tenant-copywriter",
            "connector_revision": "connector-r7",
        }
        persisted = "\n".join(
            [
                run.model_dump_json(),
                results.results[0].model_dump_json(),
                result.model_dump_json(),
            ]
        )
        assert "connector-test-secret-marker" not in persisted
        assert "connector.invalid" not in persisted

        turn_id = formation_turn_id(
            tenant_id="tenant-1",
            user_id="operator-1",
            session_id="connector-session",
            request_id="connector-request",
            run_id=result.run_id,
        )
        trace = await trace_repository.list_events(
            ExecutionTraceQuery(
                tenant_id="tenant-1",
                user_id="operator-1",
                session_id="connector-session",
                turn_id=turn_id,
            )
        )
        assert len(trace) == 1
        assert "connector-test-secret-marker" not in trace[0].model_dump_json()
        assert "connector.invalid" not in trace[0].model_dump_json()
        assert "tenant-copywriter" not in trace[0].model_dump_json()
        assert resolver.released == [connector]
        assert private_connection.closed is True
    finally:
        await catalog.aclose()


async def test_connector_resolution_consumes_the_pipeline_deadline_before_run_acceptance() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    connector = _connector()
    resolver = _DeadlineAdvancingConnectorResolver(
        connector=connector,
        clock=clock,
        advance_by=timedelta(seconds=2),
    )
    adapter = _RecordingRuntimeAdapter()
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(default_deadline_seconds=1),
            clock=clock,
        ),
    )
    try:
        with pytest.raises(InvocationDeadlineExceededError) as raised:
            await service.invoke(_request())

        expected_deadline = datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC)
        assert raised.value.status_code == 504
        assert resolver.requests[0].deadline_at == expected_deadline
        assert resolver.released == [connector]
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
    finally:
        await catalog.aclose()


async def test_same_turn_connector_failure_after_deadline_is_the_stable_504() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    resolver = _DeadlineAdvancingConnectorResolver(
        connector=None,
        failure=RuntimeError("resolver failed after budget was consumed"),
        clock=clock,
        advance_by=timedelta(seconds=2),
    )
    adapter = _RecordingRuntimeAdapter()
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(default_deadline_seconds=1),
            clock=clock,
        ),
    )
    try:
        with pytest.raises(InvocationDeadlineExceededError) as raised:
            await service.invoke(_request())

        assert raised.value.status_code == 504
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
    finally:
        await catalog.aclose()


async def test_post_deadline_connector_release_is_observed_without_delaying_504() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    connector = _connector()
    resolver = _BlockingReleaseDeadlineConnectorResolver(
        connector=connector,
        clock=clock,
        advance_by=timedelta(seconds=2),
    )
    adapter = _RecordingRuntimeAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(default_deadline_seconds=1),
        clock=clock,
    )
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        runtime=runtime,
    )
    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await asyncio.wait_for(service.invoke(_request()), timeout=0.1)

        await asyncio.wait_for(resolver.release_started.wait(), timeout=1)
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
        assert runtime._late_preflight_cleanup_tasks

        resolver.allow_release.set()
        for _ in range(20):
            if not runtime._late_preflight_cleanup_tasks:
                break
            await asyncio.sleep(0)
        assert connector.connection.closed is True
        assert runtime._late_preflight_cleanup_tasks == set()
    finally:
        resolver.allow_release.set()
        await runtime.stop()
        await catalog.aclose()


async def test_shutdown_does_not_wait_forever_for_a_cancellation_defiant_connector_release() -> (
    None
):
    connector = _connector()
    resolver = _CancellationDefiantReleaseConnectorResolver(connector=connector)
    adapter = _RecordingRuntimeAdapter()
    runtime = InvocationRuntime(policy=InvocationRuntimePolicy(default_deadline_seconds=0.02))
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        runtime=runtime,
    )
    try:
        result = await asyncio.wait_for(service.invoke(_request()), timeout=0.2)

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "invocation_deadline_exceeded"
        assert result.error.details == {
            "category": "deadline_exceeded",
            "retryable": False,
            "completion_certainty": "unknown",
        }
        assert len(runs.runs) == len(results.results) == 1
        assert results.results[0].error == result.error.model_dump()
        await asyncio.wait_for(resolver.release_started.wait(), timeout=1)
        assert runtime._late_preflight_cleanup_tasks

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runtime.stop(), timeout=0.03)
        assert resolver.release_cancelled.is_set()
        assert runtime._late_preflight_cleanup_tasks

        resolver.allow_release.set()
        for _ in range(20):
            if not runtime._late_preflight_cleanup_tasks:
                break
            await asyncio.sleep(0)
        assert connector.connection.closed is True
        assert runtime._late_preflight_cleanup_tasks == set()
    finally:
        resolver.allow_release.set()
        await runtime.stop()
        await catalog.aclose()


async def test_deadline_observes_and_releases_a_late_connector_resolution() -> None:
    connector = _connector()
    resolver = _DeadlineDefiantConnectorResolver(connector=connector)
    adapter = _RecordingRuntimeAdapter()
    runtime = InvocationRuntime(policy=InvocationRuntimePolicy(default_deadline_seconds=0.01))
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        runtime=runtime,
    )
    try:
        invocation = asyncio.create_task(service.invoke(_request()))
        await asyncio.wait_for(resolver.resolution_started.wait(), timeout=1)

        with pytest.raises(InvocationDeadlineExceededError):
            await invocation

        await asyncio.wait_for(resolver.deadline_cancelled.wait(), timeout=1)
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
        assert resolver.released == []
        assert len(runtime._late_preflight_tasks) == 1

        resolver.allow_finish.set()
        for _ in range(20):
            if resolver.released:
                break
            await asyncio.sleep(0)
        assert resolver.released == [connector]
        assert connector.connection.closed is True
        assert runtime._late_preflight_tasks == set()
    finally:
        resolver.allow_finish.set()
        await runtime.stop()
        await catalog.aclose()


async def test_deadline_cancels_and_observes_late_connector_preparation_before_release() -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _DeadlineDefiantConnectorPreparerAdapter()
    runtime = InvocationRuntime(policy=InvocationRuntimePolicy(default_deadline_seconds=0.01))
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        runtime=runtime,
    )
    try:
        invocation = asyncio.create_task(service.invoke(_request()))
        await asyncio.wait_for(adapter.preparation_started.wait(), timeout=1)

        with pytest.raises(InvocationDeadlineExceededError):
            await invocation

        await asyncio.wait_for(adapter.deadline_cancelled.wait(), timeout=1)
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
        assert resolver.released == []
        assert len(runtime._late_preflight_tasks) == 1

        adapter.allow_finish.set()
        for _ in range(20):
            if resolver.released:
                break
            await asyncio.sleep(0)
        assert resolver.released == [connector]
        assert connector.connection.closed is True
        assert runtime._late_preflight_tasks == set()
    finally:
        adapter.allow_finish.set()
        await runtime.stop()
        await catalog.aclose()


async def test_client_disconnect_keeps_accepted_connector_scope_until_run_converges() -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _BlockingRuntimeAdapter()
    runtime = InvocationRuntime(policy=InvocationRuntimePolicy(default_deadline_seconds=5))
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        runtime=runtime,
    )
    try:
        client_waiter = asyncio.create_task(service.invoke(_request()))
        await asyncio.wait_for(adapter.execution_started.wait(), timeout=1)

        client_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client_waiter

        assert resolver.released == []
        adapter.allow_completion.set()
        for _ in range(20):
            if results.results and not runtime._accepted_execution_tasks:
                break
            await asyncio.sleep(0)

        assert len(runs.runs) == len(results.results) == 1
        assert results.results[0].status == "completed"
        assert resolver.released == [connector]
        assert connector.connection.closed is True
    finally:
        adapter.allow_completion.set()
        await runtime.stop()
        await catalog.aclose()


@pytest.mark.parametrize(
    ("resolver", "reason_code", "released"),
    [
        (None, "connector_unavailable", False),
        (_RecordingConnectorResolver(connector=None), "connector_unavailable", False),
        (
            _RecordingConnectorResolver(
                connector=_connector(tenant_id="tenant-2"),
            ),
            "connector_unauthorized",
            True,
        ),
        (
            _RecordingConnectorResolver(
                connector=_connector(adapter_key="other-adapter"),
            ),
            "connector_adapter_incompatible",
            True,
        ),
        (
            _RecordingConnectorResolver(
                connector=_connector(connector_ref="other-connector"),
            ),
            "connector_reference_invalid",
            True,
        ),
        (
            _RecordingConnectorResolver(
                connector=_connector(revision="https://private.invalid/token"),
            ),
            "connector_revision_invalid",
            True,
        ),
    ],
)
async def test_connector_missing_or_mismatched_fails_closed_before_run(
    resolver: _RecordingConnectorResolver | None,
    reason_code: str,
    released: bool,
) -> None:
    adapter = _RecordingRuntimeAdapter()
    catalog, service, runs, results = await _service(resolver=resolver, adapter=adapter)
    try:
        with pytest.raises(InvocationBindingUnavailableError) as exc_info:
            await service.invoke(_request())

        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "invocation_binding_unavailable"
        assert exc_info.value.details == {"reason_code": reason_code}
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
        if resolver is not None:
            assert bool(resolver.released) is released
    finally:
        await catalog.aclose()


async def test_connector_resolver_failure_is_safe_and_does_not_accept_a_run() -> None:
    resolver = _RecordingConnectorResolver(
        connector=_connector(),
        failure=InvocationBindingUnavailableError(
            "https://connector.invalid/?authorization=connector-test-secret-marker",
            details={"endpoint": "https://connector.invalid/private-operation"},
        ),
    )
    adapter = _RecordingRuntimeAdapter()
    catalog, service, runs, results = await _service(resolver=resolver, adapter=adapter)
    try:
        with pytest.raises(InvocationBindingUnavailableError) as exc_info:
            await service.invoke(_request())

        assert exc_info.value.details == {"reason_code": "connector_unavailable"}
        assert exc_info.value.__cause__ is None
        assert "connector-test-secret-marker" not in str(exc_info.value)
        assert "connector.invalid" not in str(exc_info.value)
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
        assert resolver.released == []
    finally:
        await catalog.aclose()


async def test_connector_scope_rechecks_the_current_principal_before_resolution() -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _RecordingRuntimeAdapter()
    catalog, service, runs, results = await _service(resolver=resolver, adapter=adapter)
    try:
        accepted_request = _request()
        selection = service.snapshot_runtime.preflight_for_user(
            accepted_request.agent_id,
            accepted_request.user,
        )
        assert selection is not None
        unauthorized_invocation = AgentInvocation(
            run_id="connector-unauthorized-run",
            session_id=accepted_request.session_id,
            agent_id=accepted_request.agent_id,
            user={
                "id": "viewer-1",
                "roles": ["viewer"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            input={"text": "attempt a rebinding"},
        )

        with pytest.raises(InvocationBindingUnavailableError) as exc_info:
            await service._invoke_resolved_binding(
                service.resolve_direct_binding(selection),
                unauthorized_invocation,
            )

        assert exc_info.value.details == {"reason_code": "connector_unauthorized"}
        assert resolver.requests == []
        assert resolver.released == []
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_connector_releases_after_post_resolution_preflight_rejection() -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _RecordingRuntimeAdapter()
    catalog, service, runs, results = await _service(
        resolver=resolver,
        adapter=adapter,
        definition=_definition(required_knowledge=True),
    )
    try:
        with pytest.raises(InvocationPreflightRejectedError) as exc_info:
            await service.invoke(_request())

        assert exc_info.value.code == "invocation_required_context_missing"
        assert runs.runs == {}
        assert results.results == []
        assert adapter.calls == []
        assert resolver.released == [connector]
    finally:
        await catalog.aclose()


async def test_connector_releases_after_adapter_cancellation() -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _RecordingRuntimeAdapter(failure=asyncio.CancelledError())
    catalog, service, runs, results = await _service(resolver=resolver, adapter=adapter)
    try:
        result = await service.invoke(_request())

        assert result.status == "failed"
        assert len(runs.runs) == len(results.results) == 1
        assert len(adapter.calls) == 1
        assert resolver.released == [connector]
    finally:
        await catalog.aclose()


async def test_connector_releases_when_preacceptance_preparation_is_cancelled() -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _BlockingConnectorPreparerAdapter()
    catalog, service, runs, results = await _service(resolver=resolver, adapter=adapter)
    try:
        invocation_task = asyncio.create_task(service.invoke(_request()))
        await asyncio.wait_for(adapter.preparation_started.wait(), timeout=1)
        invocation_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await invocation_task

        assert resolver.released == [connector]
        assert connector.connection.closed is True
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        adapter.allow_preparation.set()
        await catalog.aclose()


@pytest.mark.parametrize(
    ("outcome", "sleep_seconds", "uses_deadline"),
    [
        (
            RawInvocationOutcome(
                failure=RawInvocationFailure.for_category("remote_failure", retryable=False)
            ),
            0.0,
            False,
        ),
        (
            RawInvocationOutcome(output={"summary": "late"}),
            0.1,
            True,
        ),
    ],
)
async def test_connector_releases_after_accepted_failure_and_deadline(
    outcome: RawInvocationOutcome,
    sleep_seconds: float,
    uses_deadline: bool,
) -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _RecordingRuntimeAdapter(outcome=outcome, sleep_seconds=sleep_seconds)
    catalog, service, runs, results = await _service(resolver=resolver, adapter=adapter)
    try:
        request = _request()
        deadline_at = datetime.now(UTC) + timedelta(milliseconds=50) if uses_deadline else None
        invocation = AgentInvocation(
            run_id="connector-deadline-run",
            request_id=request.request_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            user=request.user,
            input={"text": "write the approved output"},
            deadline_at=deadline_at,
        )
        selection = service.snapshot_runtime.preflight_for_user(
            invocation.agent_id, invocation.user
        )
        assert selection is not None
        result = await service._invoke_resolved_binding(
            service.resolve_direct_binding(selection),
            invocation,
        )

        assert result.status == "failed"
        assert len(runs.runs) == len(results.results) == 1
        assert len(adapter.calls) == 1
        if uses_deadline:
            for _ in range(10):
                if resolver.released:
                    break
                await asyncio.sleep(0)
        assert resolver.released == [connector]
    finally:
        await catalog.aclose()


async def test_late_connector_adapter_is_strongly_drained_before_release() -> None:
    connector = _connector()
    resolver = _RecordingConnectorResolver(connector=connector)
    adapter = _CancellationDefiantConnectorAdapter()
    catalog, service, runs, results = await _service(resolver=resolver, adapter=adapter)
    try:
        request = _request()
        invocation = AgentInvocation(
            run_id="connector-defiant-deadline-run",
            request_id=request.request_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            user=request.user,
            input={"text": "write the approved output"},
            deadline_at=datetime.now(UTC) + timedelta(milliseconds=50),
        )
        selection = service.snapshot_runtime.preflight_for_user(
            invocation.agent_id, invocation.user
        )
        assert selection is not None

        result = await service._invoke_resolved_binding(
            service.resolve_direct_binding(selection),
            invocation,
        )

        assert result.status == "failed"
        await asyncio.wait_for(adapter.deadline_cancelled.wait(), timeout=1)
        assert len(runs.runs) == len(results.results) == 1
        assert resolver.released == []
        gc.collect()
        await asyncio.sleep(0)
        runtime = service.invocation_runtime
        assert len(runtime._late_adapter_tasks) == 1

        stop_task = asyncio.create_task(runtime.stop())
        await asyncio.wait_for(adapter.shutdown_cancelled.wait(), timeout=1)
        await asyncio.wait_for(stop_task, timeout=1)

        assert resolver.released == [connector]
        assert connector.connection.closed is True
        assert runtime._late_adapter_tasks == set()
        assert runtime._late_adapter_cleanup_tasks == set()
    finally:
        await catalog.aclose()
