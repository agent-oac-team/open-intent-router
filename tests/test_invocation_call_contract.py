import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.errors import InvocationDeadlineExceededError, InvocationPreflightRejectedError
from app.repositories.canonical_invocations import MemoryCanonicalInvocationStore
from app.repositories.database import DatabasePlanRepository
from app.repositories.memory import (
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import MemoryTurnRepository
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
    RawInvocationOutcome,
    RuntimeAdapterBinding,
)
from app.schemas.agent_context import AgentRuntimeContext
from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import ArtifactRef
from app.schemas.invocation import AgentInvocation, InvokeRequest
from app.schemas.logs import AgentRun
from app.schemas.plans import Plan
from app.schemas.turns import TurnUserInput
from app.services.agent_context_service import (
    AgentContextAssemblyService,
    KnowledgeRequirementError,
)
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.plan_service import PlanService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime
from app.services.turn_service import TurnService


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


@dataclass
class _RecordingAdapter:
    response: RawInvocationOutcome = field(
        default_factory=lambda: RawInvocationOutcome(output={"summary": "completed"})
    )
    calls: list[AgentCallEnvelope] = field(default_factory=list)
    delay_seconds: float = 0.0

    async def execute(
        self,
        _binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append(envelope)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.response


@dataclass
class _CancellationIgnoringAdapter(_RecordingAdapter):
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append(envelope)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.1)
            self.finished.set()
        return self.response


@dataclass
class _LifecycleTrackedLateAdapter(_RecordingAdapter):
    first_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append(envelope)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.first_cancelled.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Deliberately hostile Adapter code: lifecycle ownership must
                # still retain it rather than losing its exception/task.
                continue
        return self.response


@dataclass
class _DeadlineTamperingAdapter(_RecordingAdapter):
    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append(envelope)
        time.sleep(0.03)
        # Adapter authors cannot assign to the frozen Envelope, but Core must
        # still be correct if extension code bypasses Pydantic deliberately.
        object.__setattr__(envelope, "deadline_at", datetime.now(UTC) + timedelta(seconds=60))
        return self.response


class _RequiredContextRejectingAssembler:
    async def assemble(self, **_kwargs: object) -> object:
        raise KnowledgeRequirementError(
            "knowledge_not_found",
            "Required Knowledge Context has no usable items",
        )


@dataclass
class _RecordingContextAssembler:
    inputs: list[dict[str, object]] = field(default_factory=list)

    async def assemble(self, **kwargs: object) -> AgentRuntimeContext:
        self.inputs.append(dict(kwargs["invocation_input"]))
        return AgentRuntimeContext()


@dataclass
class _Clock:
    """Deterministic UTC clock shared by every invocation pipeline phase."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, duration: timedelta) -> None:
        self.now += duration


@dataclass
class _DeadlineAdvancingContextAssembler:
    """Consumes controlled budget before the Adapter can be accepted."""

    clock: _Clock
    advance_by: timedelta
    deadlines: list[datetime] = field(default_factory=list)

    async def assemble(self, **kwargs: object) -> AgentRuntimeContext:
        deadline_at = kwargs["deadline_at"]
        assert isinstance(deadline_at, datetime)
        self.deadlines.append(deadline_at)
        self.clock.advance(self.advance_by)
        return AgentRuntimeContext()


@dataclass
class _DeadlineAdvancingFailureAdapter(_RecordingAdapter):
    """Raises after consuming the last Adapter budget in one event-loop turn."""

    clock: _Clock | None = None
    advance_by: timedelta = timedelta(0)

    async def execute(
        self,
        _binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append(envelope)
        if self.clock is not None:
            self.clock.advance(self.advance_by)
        raise RuntimeError("adapter failed after the deadline")


@dataclass
class _BlockingAdapter(_RecordingAdapter):
    """Lets a test cancel only the client waiter after Run acceptance."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    allow_completion: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(
        self,
        _binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append(envelope)
        self.started.set()
        await self.allow_completion.wait()
        return self.response


@dataclass
class _AcknowledgementDelayedCompletionStore:
    """Expose a post-commit acknowledgement delay without changing storage."""

    delegate: object
    clock: _Clock
    advance_by: timedelta

    async def complete(self, **kwargs: object):
        stored = await self.delegate.complete(**kwargs)  # type: ignore[attr-defined]
        self.clock.advance(self.advance_by)
        return stored


@dataclass
class _DeadlineAdvancingCanonicalStore:
    """Make a competing canonical start acknowledge after this caller expires."""

    delegate: MemoryCanonicalInvocationStore
    clock: _Clock
    advance_by: timedelta

    async def start_run(self, *args: object, **kwargs: object):
        stored = await self.delegate.start_run(*args, **kwargs)
        self.clock.advance(self.advance_by)
        return stored

    async def complete_run(self, *args: object, **kwargs: object):
        return await self.delegate.complete_run(*args, **kwargs)


def _definition(
    *,
    limits: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
    principal_projection: dict[str, object] | None = None,
    input_schema: dict[str, object] | None = None,
    output_schema: dict[str, object] | None = None,
) -> AgentDefinitionV2:
    handling: dict[str, object] = {
        "kind": "invocation",
        "adapter_key": "contract_adapter",
        "config": {"operation": "execute"},
    }
    if limits is not None:
        handling["limits"] = limits
    if principal_projection is not None:
        handling["principal_projection"] = principal_projection
    definition: dict[str, object] = {
        "schema_version": "oir-agent-v2",
        "agent_id": "contract-agent",
        "name": "Contract Agent",
        "description": "Exercises the Runtime call contract.",
        "revision": 1,
        "access_policy": {"allow_roles": ["operator"]},
        "input_schema": input_schema
        or {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "output_schema": output_schema
        or {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
        "handling": handling,
    }
    if context is not None:
        definition["context"] = context
    return AgentDefinitionV2.model_validate(definition)


async def _service(
    *,
    adapter: _RecordingAdapter,
    runtime: InvocationRuntime,
    definition: AgentDefinitionV2 | None = None,
    accepted_principal_claims: frozenset[str] = frozenset(),
    accepted_principal_attribute_keys: frozenset[str] = frozenset(),
    plan_service: PlanService | None = None,
    agent_context_service: object | None = None,
):
    catalog = await RuntimeCatalog.activate(
        [
            RuntimeAdapterDescriptor(
                key="contract_adapter",
                contract_version="contract-v1",
                implementation_version="contract-implementation-v1",
                config_schema={
                    "type": "object",
                    "required": ["operation"],
                    "properties": {"operation": {"const": "execute"}},
                },
                capability=RuntimeAdapterCapability(
                    invocation=True,
                    v2_invocation=True,
                    invocation_runtime=True,
                    accepted_principal_claims=accepted_principal_claims,
                    accepted_principal_attribute_keys=accepted_principal_attribute_keys,
                ),
                factory=lambda _context: adapter,
                health_check=_healthy,
                lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
            )
        ],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load([definition or _definition()], source="test")
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    return (
        catalog,
        InvocationService(
            registry=object(),
            run_repository=runs,
            result_repository=results,
            snapshot_runtime=snapshot_runtime,
            binding_resolver=BindingResolver(catalog),
            invocation_runtime=runtime,
            plan_service=plan_service,
            agent_context_service=agent_context_service,
        ),
        runs,
        results,
    )


def _request(
    *,
    text: str,
    memory_context: dict[str, object] | None = None,
    artifact_refs: list[dict[str, object]] | None = None,
    user: dict[str, object] | None = None,
) -> InvokeRequest:
    payload: dict[str, object] = {
        "request_id": "contract-request",
        "session_id": "contract-session",
        "agent_id": "contract-agent",
        "user": user
        or {
            "id": "operator-1",
            "roles": ["operator"],
            "attributes": {"tenant_id": "tenant-1"},
        },
        "input": {"text": text},
    }
    if memory_context is not None:
        payload["memory_context"] = memory_context
    if artifact_refs is not None:
        payload["artifact_refs"] = artifact_refs
    return InvokeRequest.model_validate(payload)


def test_deployment_policy_supplies_runtime_hard_limits_and_claim_allowlists() -> None:
    policy = InvocationRuntimePolicy.from_settings(
        Settings(
            storage_backend="memory",
            invocation_max_input_bytes=111,
            invocation_max_context_bytes=222,
            invocation_max_message_chars=33,
            invocation_max_output_bytes=444,
            invocation_max_artifact_count=5,
            invocation_max_artifact_metadata_bytes=66,
            invocation_allowed_principal_claims="roles, entitlements, unknown",
            invocation_allowed_principal_attribute_keys=(
                "region, department, token, api_key,apikey,cookie_value, https://invalid"
            ),
        )
    )

    assert policy.max_input_bytes == 111
    assert policy.max_context_bytes == 222
    assert policy.max_message_chars == 33
    assert policy.max_output_bytes == 444
    assert policy.max_artifact_count == 5
    assert policy.max_artifact_metadata_bytes == 66
    assert policy.allowed_principal_claims == frozenset({"roles", "entitlements"})
    assert policy.allowed_principal_attribute_keys == frozenset({"region", "department"})


def test_nonempty_definition_call_contract_survives_handling_serialization() -> None:
    handling = _definition(
        limits={"max_input_bytes": 64, "max_artifact_count": 2},
        principal_projection={"claims": ["roles"], "attribute_keys": ["region"]},
    ).handling

    assert handling.model_dump(mode="json", exclude_none=True) == {
        "kind": "invocation",
        "adapter_key": "contract_adapter",
        "config": {"operation": "execute"},
        "limits": {"max_input_bytes": 64, "max_artifact_count": 2},
        "principal_projection": {"claims": ["roles"], "attribute_keys": ["region"]},
    }


async def test_input_limit_rejects_before_direct_invocation_creates_a_run() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=32)),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(_request(text="x" * 128))

    assert raised.value.code == "invocation_input_limit_exceeded"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_invalid_declared_input_rejects_before_direct_invocation_creates_a_run() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )
    request = _request(text="within the input limit").model_copy(
        update={"input": {"headers": {"authorization": "Bearer never-forward"}}}
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(request)

    assert raised.value.code == "invocation_input_invalid"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_nested_host_or_credential_input_rejects_before_acceptance() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        definition=_definition(
            input_schema={
                "type": "object",
                "required": ["payload"],
                "properties": {"payload": {"type": "object"}},
            }
        ),
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )
    request = _request(text="unused").model_copy(
        update={
            "input": {
                "payload": {"Authorization": "Bearer nested-credential-must-not-reach-adapter"}
            }
        }
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(request)

    assert raised.value.code == "invocation_input_invalid"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_accepted_message_limit_becomes_one_safe_invalid_response() -> None:
    adapter = _RecordingAdapter(
        response=RawInvocationOutcome(message="x" * 64, output={"summary": "completed"})
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(max_input_bytes=512, max_message_chars=16)
        ),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert len(adapter.calls) == 1
    assert len(runs.runs) == len(results.results) == 1
    assert (await runs.get_run(result.run_id)).status == "failed"

    await catalog.aclose()


async def test_absolute_deadline_bounds_an_accepted_adapter_call() -> None:
    adapter = _RecordingAdapter(delay_seconds=0.1)
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=0.01,
            )
        ),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_deadline_exceeded"
    assert result.error.details == {
        "category": "deadline_exceeded",
        "retryable": False,
        "completion_certainty": "unknown",
    }
    assert len(adapter.calls) == 1
    assert adapter.calls[0].deadline_at.tzinfo is not None
    assert len(runs.runs) == len(results.results) == 1
    run = await runs.get_run(result.run_id)
    assert run is not None
    assert run.deadline_at == adapter.calls[0].deadline_at
    assert run.error == result.error.model_dump()
    assert results.results[0].error == result.error.model_dump()

    await catalog.aclose()


async def test_terminal_commit_before_deadline_keeps_its_durable_success_after_ack_delay() -> None:
    """A late acknowledgement cannot turn an on-time terminal commit into a lie."""

    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(max_input_bytes=512, default_deadline_seconds=1),
        clock=clock,
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    service.completion_store = _AcknowledgementDelayedCompletionStore(
        delegate=service.completion_store,
        clock=clock,
        advance_by=timedelta(seconds=2),
    )

    try:
        result = await service.invoke(_request(text="commit before acknowledgement"))

        assert result.status == "completed"
        assert result.error is None
        stored_run = await runs.get_run(result.run_id)
        assert stored_run is not None and stored_run.status == "completed"
        assert len(results.results) == 1
    finally:
        await catalog.aclose()


async def test_pipeline_stamps_one_deadline_before_context_and_never_resets_it() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    assembler = _DeadlineAdvancingContextAssembler(
        clock=clock,
        advance_by=timedelta(milliseconds=900),
    )
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=1,
        ),
        clock=clock,
    )
    catalog, service, runs, _results = await _service(
        adapter=adapter,
        runtime=runtime,
        agent_context_service=assembler,
    )

    try:
        result = await service.invoke(_request(text="within the input limit"))

        expected_deadline = datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC)
        assert result.status == "completed"
        assert assembler.deadlines == [expected_deadline]
        assert [call.deadline_at for call in adapter.calls] == [expected_deadline]
        run = await runs.get_run(result.run_id)
        assert run is not None
        assert run.deadline_at == expected_deadline
    finally:
        await catalog.aclose()


async def test_context_preflight_expiry_returns_504_without_accepting_a_run() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    assembler = _DeadlineAdvancingContextAssembler(
        clock=clock,
        advance_by=timedelta(seconds=2),
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=1,
            ),
            clock=clock,
        ),
        agent_context_service=assembler,
    )

    try:
        with pytest.raises(InvocationDeadlineExceededError) as raised:
            await service.invoke(_request(text="within the input limit"))

        assert raised.value.status_code == 504
        assert raised.value.code == "invocation_deadline_exceeded"
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_context_remaining_budget_timeout_maps_to_the_stable_preaccept_504() -> None:
    adapter = _RecordingAdapter()
    context_service = AgentContextAssemblyService(
        Settings(storage_backend="memory"),
        memory_service=object(),
    )

    async def slow_pipeline(**_kwargs: object) -> object:
        await asyncio.sleep(0.1)
        raise AssertionError("Context pipeline should have timed out")

    context_service.pipeline.assemble = slow_pipeline  # type: ignore[method-assign]
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(max_input_bytes=512, default_deadline_seconds=0.01)
        ),
        agent_context_service=context_service,
    )

    try:
        with pytest.raises(InvocationDeadlineExceededError) as raised:
            await service.invoke(_request(text="within the input limit"))

        assert raised.value.status_code == 504
        assert raised.value.code == "invocation_deadline_exceeded"
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_slow_plan_preflight_consumes_the_same_deadline_before_run_acceptance() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=1,
            ),
            clock=clock,
        ),
    )

    async def slow_active_plan(*_args: object) -> None:
        clock.advance(timedelta(seconds=2))
        return None

    service._active_plan_for_invocation = slow_active_plan  # type: ignore[method-assign]
    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await service.invoke(_request(text="within the input limit"))

        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_accepted_pre_dispatch_deadline_is_certain_and_skips_adapter_call() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=1,
        ),
        clock=clock,
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    original_publish = service._publish_run
    advanced = False

    async def advance_after_acceptance(*args: object, **kwargs: object) -> None:
        nonlocal advanced
        if not advanced:
            advanced = True
            clock.advance(timedelta(seconds=2))
        await original_publish(*args, **kwargs)

    service._publish_run = advance_after_acceptance  # type: ignore[method-assign]
    try:
        result = await service.invoke(_request(text="within the input limit"))

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "invocation_deadline_exceeded"
        assert result.error.details == {
            "category": "deadline_exceeded",
            "retryable": False,
            "completion_certainty": "certain",
        }
        assert adapter.calls == []
        assert len(runs.runs) == len(results.results) == 1
        run = await runs.get_run(result.run_id)
        assert run is not None
        assert run.deadline_at == datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC)
        assert run.error == result.error.model_dump()
        assert results.results[0].error == result.error.model_dump()
    finally:
        await catalog.aclose()


async def test_late_terminal_write_converges_to_durable_unknown_deadline_failure() -> None:
    """A completion write cannot commit an Adapter success after its deadline."""

    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=1,
            ),
            clock=clock,
        ),
    )
    original_complete = service.completion_store.complete
    completion_attempts = 0

    async def advance_before_terminal_commit(*args: object, **kwargs: object):
        nonlocal completion_attempts
        completion_attempts += 1
        clock.advance(timedelta(seconds=2))
        return await original_complete(*args, **kwargs)

    service.completion_store.complete = advance_before_terminal_commit  # type: ignore[method-assign]
    try:
        result = await service.invoke(_request(text="within the input limit"))

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "invocation_deadline_exceeded"
        assert result.error.details == {
            "category": "deadline_exceeded",
            "retryable": False,
            "completion_certainty": "unknown",
        }
        assert len(adapter.calls) == 1

        for _ in range(20):
            if results.results:
                break
            await asyncio.sleep(0)
        assert completion_attempts >= 2
        assert len(runs.runs) == len(results.results) == 1
        stored_run = next(iter(runs.runs.values()))
        assert stored_run.status == "failed"
        assert stored_run.error == result.error.model_dump()
        assert results.results[0].status == "failed"
        assert results.results[0].error == result.error.model_dump()
    finally:
        await catalog.aclose()


async def test_same_turn_adapter_exception_after_deadline_is_unknown_deadline_failure() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _DeadlineAdvancingFailureAdapter(
        clock=clock,
        advance_by=timedelta(seconds=2),
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=1,
            ),
            clock=clock,
        ),
    )
    try:
        result = await service.invoke(_request(text="within the input limit"))

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "invocation_deadline_exceeded"
        assert result.error.details == {
            "category": "deadline_exceeded",
            "retryable": False,
            "completion_certainty": "unknown",
        }
        assert len(adapter.calls) == 1
        assert len(runs.runs) == len(results.results) == 1
        assert next(iter(runs.runs.values())).error == result.error.model_dump()
    finally:
        await catalog.aclose()


async def test_client_disconnect_does_not_cancel_an_accepted_run() -> None:
    adapter = _BlockingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=5,
        )
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    try:
        client_waiter = asyncio.create_task(service.invoke(_request(text="within the input limit")))
        await asyncio.wait_for(adapter.started.wait(), timeout=1)

        client_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client_waiter

        adapter.allow_completion.set()
        for _ in range(20):
            if results.results and not runtime._accepted_execution_tasks:
                break
            await asyncio.sleep(0)

        assert len(adapter.calls) == 1
        assert len(runs.runs) == len(results.results) == 1
        completed_run = next(iter(runs.runs.values()))
        assert completed_run.status == "completed"
        assert results.results[0].status == "completed"
        assert runtime._accepted_execution_tasks == set()
    finally:
        adapter.allow_completion.set()
        await catalog.aclose()


async def test_client_disconnect_during_run_acceptance_keeps_the_commit_window_owned() -> None:
    """A cancellation must not split durable Run creation from convergence."""

    adapter = _BlockingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=5,
        )
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    add_started = asyncio.Event()
    allow_add = asyncio.Event()
    original_add = runs.add_run

    async def gated_add(run):
        add_started.set()
        await allow_add.wait()
        return await original_add(run)

    runs.add_run = gated_add  # type: ignore[method-assign]
    try:
        client_waiter = asyncio.create_task(service.invoke(_request(text="within the input limit")))
        await asyncio.wait_for(add_started.wait(), timeout=1)

        client_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client_waiter

        # The task is retained even though the durable insert was still
        # awaiting: it now owns both possible commit outcomes.
        assert runtime._accepted_execution_tasks
        allow_add.set()
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        adapter.allow_completion.set()
        for _ in range(20):
            if results.results and not runtime._accepted_execution_tasks:
                break
            await asyncio.sleep(0)

        assert len(adapter.calls) == 1
        assert len(runs.runs) == len(results.results) == 1
        assert next(iter(runs.runs.values())).status == "completed"
    finally:
        allow_add.set()
        adapter.allow_completion.set()
        await catalog.aclose()


async def test_deadline_during_run_start_rolls_back_the_preacceptance_mutation() -> None:
    """A start write that reaches the deadline cannot leave a durable Run."""

    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(max_input_bytes=512, default_deadline_seconds=1),
        clock=clock,
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    original_add = runs.add_run

    async def write_then_expire(run):
        stored = await original_add(run)
        clock.advance(timedelta(seconds=2))
        return stored

    runs.add_run = write_then_expire  # type: ignore[method-assign]
    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await service.invoke(_request(text="within the input limit"))

        for _ in range(20):
            if not runtime._durable_start_reconciliation_tasks:
                break
            await asyncio.sleep(0)
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_shutdown_reconciles_a_run_committed_before_start_acknowledgement() -> None:
    """A cancellation after ``add_run`` commits still produces one terminal Result."""

    adapter = _BlockingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=5,
        )
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    committed = asyncio.Event()
    original_add = runs.add_run

    async def commit_then_block(run):
        stored = await original_add(run)
        committed.set()
        await asyncio.Future()
        return stored

    runs.add_run = commit_then_block  # type: ignore[method-assign]
    try:
        client_waiter = asyncio.create_task(service.invoke(_request(text="within the input limit")))
        await asyncio.wait_for(committed.wait(), timeout=1)

        client_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client_waiter
        assert runtime._accepted_execution_tasks

        await asyncio.wait_for(runtime.stop(), timeout=1)

        assert len(runs.runs) == len(results.results) == 1
        terminal_run = next(iter(runs.runs.values()))
        assert terminal_run.status == "failed"
        assert results.results[0].status == "failed"
        assert runtime._accepted_execution_tasks == set()
    finally:
        adapter.allow_completion.set()
        await catalog.aclose()


async def test_shutdown_retries_a_transient_read_after_committed_run_start() -> None:
    """A read outage is not evidence that a cancelled durable start rolled back."""

    adapter = _BlockingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=5,
        )
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    committed = asyncio.Event()
    original_add = runs.add_run
    original_get = runs.get_run
    fail_first_read = True

    async def commit_then_block(run):
        stored = await original_add(run)
        committed.set()
        await asyncio.Future()
        return stored

    async def transiently_unavailable(run_id: str):
        nonlocal fail_first_read
        if fail_first_read:
            fail_first_read = False
            raise RuntimeError("temporary run read failure")
        return await original_get(run_id)

    runs.add_run = commit_then_block  # type: ignore[method-assign]
    runs.get_run = transiently_unavailable  # type: ignore[method-assign]
    try:
        client_waiter = asyncio.create_task(service.invoke(_request(text="within the input limit")))
        await asyncio.wait_for(committed.wait(), timeout=1)

        client_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client_waiter

        await asyncio.wait_for(runtime.stop(), timeout=1)

        assert len(runs.runs) == len(results.results) == 1
        assert next(iter(runs.runs.values())).status == "failed"
        assert results.results[0].status == "failed"
        assert runtime._accepted_execution_tasks == set()
    finally:
        adapter.allow_completion.set()
        await catalog.aclose()


async def test_failed_run_start_releases_its_plan_claim_only_after_a_readable_absence() -> None:
    adapter = _RecordingAdapter()
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "failed-start-plan",
                "tenant_id": "tenant-1",
                "user_id": "operator-1",
                "session_id": "contract-session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "failed-start-step",
                        "agent_id": "contract-agent",
                        "description": "Release only a definitely unstarted claim",
                    }
                ],
            }
        ),
        publish=False,
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=5,
            )
        ),
        plan_service=plan_service,
    )

    async def fail_before_commit(_run):
        raise RuntimeError("run store unavailable before commit")

    runs.add_run = fail_before_commit  # type: ignore[method-assign]
    selection = service.snapshot_runtime.snapshot.select_for_user(
        "contract-agent", _request(text="unused").user
    )
    assert selection is not None
    try:
        with pytest.raises(RuntimeError, match="run store unavailable"):
            await service.invoke_agent(
                agent_id="contract-agent",
                session_id="contract-session",
                user=_request(text="unused").user,
                input={"text": "within the input limit"},
                context={"plan_id": plan.plan_id},
                selected_binding=selection,
            )

        for _ in range(10):
            if plan.plan_id not in plans.execution_claims:
                break
            await asyncio.sleep(0)
        restored = await plan_service.get_plan(
            plan.plan_id,
            tenant_id="tenant-1",
            user_id="operator-1",
        )
        assert restored is not None
        assert restored.steps[0].status == "pending"
        assert plan.plan_id not in plans.execution_claims
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_shutdown_converges_a_detached_run_cancelled_during_publication() -> None:
    """Lifecycle stop cannot leave an accepted detached Run in ``running``."""

    adapter = _BlockingAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=5,
        )
    )
    catalog, service, runs, results = await _service(adapter=adapter, runtime=runtime)
    publish_started = asyncio.Event()
    allow_publish = asyncio.Event()
    original_publish = service._publish_run
    publish_count = 0

    async def gated_publish(*args: object, **kwargs: object) -> None:
        nonlocal publish_count
        publish_count += 1
        if publish_count == 1:
            publish_started.set()
            await allow_publish.wait()
        await original_publish(*args, **kwargs)

    service._publish_run = gated_publish  # type: ignore[method-assign]
    try:
        client_waiter = asyncio.create_task(service.invoke(_request(text="within the input limit")))
        await asyncio.wait_for(publish_started.wait(), timeout=1)

        client_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client_waiter
        assert runtime._accepted_execution_tasks

        await asyncio.wait_for(runtime.stop(), timeout=1)

        assert len(runs.runs) == len(results.results) == 1
        terminal_run = next(iter(runs.runs.values()))
        assert terminal_run.status == "failed"
        assert results.results[0].status == "failed"
        assert runtime._accepted_execution_tasks == set()
    finally:
        allow_publish.set()
        adapter.allow_completion.set()
        await catalog.aclose()


async def test_accepted_structured_output_limit_becomes_one_safe_invalid_response() -> None:
    adapter = _RecordingAdapter(
        response=RawInvocationOutcome(output={"summary": "output-" + "x" * 128})
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(max_input_bytes=512, max_output_bytes=32)
        ),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert result.output is None
    assert len(adapter.calls) == 1
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_context_limit_rejects_before_direct_invocation_creates_a_run() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(max_input_bytes=512, max_context_bytes=64)
        ),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(
            _request(
                text="within the input limit",
                memory_context={
                    "status": "ok",
                    "items": [
                        {
                            "memory_id": "memory-contract-1",
                            "scope": "stable_fact",
                            "content": "memory-body-" + "x" * 256,
                        }
                    ],
                },
            )
        )

    assert raised.value.code == "invocation_context_limit_exceeded"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_required_knowledge_context_rejects_before_direct_invocation_creates_a_run() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        definition=_definition(
            context={"knowledge": {"mode": "prefetch", "requirement": "required"}}
        ),
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(_request(text="within the input limit"))

    assert raised.value.code == "invocation_required_context_missing"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_context_assembler_required_knowledge_rejection_is_a_safe_preflight_response() -> (
    None
):
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        definition=_definition(
            context={"knowledge": {"mode": "prefetch", "requirement": "required"}}
        ),
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
        agent_context_service=_RequiredContextRejectingAssembler(),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(_request(text="within the input limit"))

    assert raised.value.code == "invocation_required_context_missing"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_invalid_context_locator_rejects_safely_before_creating_a_run() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(
            _request(
                text="within the input limit",
                memory_context={
                    "status": "ok",
                    "items": [
                        {
                            "memory_id": "memory-1",
                            "scope": "x" * 65,
                            "content": "must-not-cross",
                        }
                    ],
                },
            )
        )

    assert raised.value.code == "invocation_context_invalid"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_accepted_inline_artifact_output_becomes_one_safe_invalid_response() -> None:
    secret = "artifact-inline-secret"
    adapter = _RecordingAdapter(
        response=RawInvocationOutcome(
            output={"summary": "completed"},
            artifact_refs=[
                ArtifactRef(
                    artifact_id="artifact-contract-1",
                    type="document",
                    uri=f"data:text/plain,{secret}",
                    metadata={"body": secret},
                )
            ],
        )
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert not result.artifact_refs
    assert len(adapter.calls) == 1
    assert len(runs.runs) == len(results.results) == 1
    persisted = "\n".join(
        [
            result.model_dump_json(),
            (await runs.get_run(result.run_id)).model_dump_json(),
            results.results[0].model_dump_json(),
        ]
    )
    assert secret not in persisted

    await catalog.aclose()


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "artifact://contract-1",
            "metadata": {"description": "inline body must not cross"},
        },
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "artifact://contract-1",
            "title": "inline body must not cross",
        },
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "https://example.test/Bearer-secret-inline",
        },
    ],
)
async def test_accepted_non_reference_artifact_content_becomes_invalid_response(
    artifact: dict[str, object],
) -> None:
    adapter = _RecordingAdapter(
        response=RawInvocationOutcome(
            output={"summary": "completed"},
            artifact_refs=[ArtifactRef.model_validate(artifact)],
        )
    )
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert not result.artifact_refs

    await catalog.aclose()


@pytest.mark.parametrize(
    ("policy", "metadata"),
    [
        (InvocationRuntimePolicy(max_input_bytes=512, max_artifact_count=0), {"size_bytes": 42}),
        (
            InvocationRuntimePolicy(max_input_bytes=512, max_artifact_metadata_bytes=8),
            {"label": "metadata-is-too-large"},
        ),
    ],
)
async def test_accepted_artifact_limit_violation_becomes_invalid_response(
    policy: InvocationRuntimePolicy,
    metadata: dict[str, object],
) -> None:
    adapter = _RecordingAdapter(
        response=RawInvocationOutcome(
            output={"summary": "completed"},
            artifact_refs=[
                ArtifactRef(
                    artifact_id="artifact-contract-1",
                    type="document",
                    uri="artifact://contract-1",
                    metadata=metadata,
                )
            ],
        )
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=policy),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert not result.artifact_refs
    assert len(adapter.calls) == 1
    assert len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_invalid_input_artifact_rejects_before_direct_invocation_creates_a_run() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(
            _request(
                text="within the input limit",
                artifact_refs=[
                    {
                        "artifact_id": "artifact-contract-1",
                        "type": "document",
                        "uri": "data:text/plain,inline-body-must-not-cross",
                    }
                ],
            )
        )

    assert raised.value.code == "invocation_artifact_invalid"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "artifact://contract-1",
            "metadata": {"authorization": "Bearer must-not-cross"},
        },
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "artifact://contract-1",
            "title": "inline body must not cross",
        },
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "artifact://artifact-safe-1/inline-body-must-not-cross",
        },
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "memory://token=secret-marker",
        },
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "artifact://sk-proj-abcdefghijklmnop",
        },
    ],
)
async def test_non_reference_input_artifact_rejects_before_acceptance(
    artifact: dict[str, object],
) -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(_request(text="within the input limit", artifact_refs=[artifact]))

    assert raised.value.code == "invocation_artifact_invalid"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_direct_invoke_passes_only_explicit_safe_artifact_references_to_adapter() -> None:
    adapter = _RecordingAdapter()
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    result = await service.invoke(
        _request(
            text="within the input limit",
            artifact_refs=[
                {
                    "artifact_id": "artifact-contract-1",
                    "type": "document",
                    "uri": "artifact://contract-1",
                    "metadata": {"size_bytes": 42},
                }
            ],
        )
    )

    assert result.status == "completed"
    assert len(adapter.calls) == 1
    assert [item.model_dump() for item in adapter.calls[0].artifact_refs] == [
        {
            "artifact_id": "artifact-contract-1",
            "type": "document",
            "uri": "artifact://contract-1",
            "title": None,
            "metadata": {"size_bytes": 42},
        }
    ]

    await catalog.aclose()


async def test_principal_claims_require_definition_deployment_and_adapter_approval() -> None:
    adapter = _RecordingAdapter()
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        definition=_definition(
            principal_projection={
                "claims": ["roles", "entitlements"],
                "attribute_keys": ["region", "department"],
            }
        ),
        accepted_principal_claims=frozenset({"roles", "entitlements"}),
        accepted_principal_attribute_keys=frozenset({"region", "department"}),
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                allowed_principal_claims=frozenset({"roles"}),
                allowed_principal_attribute_keys=frozenset({"region", "department"}),
            )
        ),
    )

    result = await service.invoke(
        _request(
            text="within the input limit",
            user={
                "id": "operator-1",
                "roles": [
                    "operator",
                    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                    "token-secret-marker",
                ],
                "entitlements": ["contract:invoke"],
                "attributes": {
                    "tenant_id": "tenant-1",
                    "region": "cn-north",
                    "department": "token-secret-marker",
                    "token": "principal-secret-marker",
                    "arbitrary": "must-not-reach-adapter",
                },
            },
        )
    )

    assert result.status == "completed"
    assert len(adapter.calls) == 1
    assert adapter.calls[0].principal.model_dump(mode="json", exclude_none=True) == {
        "subject": "operator-1",
        "tenant_id": "tenant-1",
        "roles": ["operator"],
        "attributes": {"region": "cn-north"},
    }
    assert "principal-secret-marker" not in adapter.calls[0].model_dump_json()
    assert "token-secret-marker" not in adapter.calls[0].model_dump_json()
    assert "must-not-reach-adapter" not in adapter.calls[0].model_dump_json()

    await catalog.aclose()


@pytest.mark.parametrize("field", ["api key", "to ken", "memory context"])
async def test_separator_variants_of_reserved_input_reject_before_acceptance(field: str) -> None:
    adapter = _RecordingAdapter()
    definition = _definition(
        input_schema={
            "type": "object",
            "required": [field],
            "properties": {field: {"type": "string"}},
        }
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        definition=definition,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )
    request = _request(text="unused").model_copy(update={"input": {field: "must-not-cross"}})

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(request)

    assert raised.value.code == "invocation_input_invalid"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_reserved_input_is_rejected_before_any_context_provider_receives_it() -> None:
    adapter = _RecordingAdapter()
    assembler = _RecordingContextAssembler()
    definition = _definition(
        input_schema={
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        }
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        definition=definition,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
        agent_context_service=assembler,
    )
    request = _request(text="unused").model_copy(
        update={"input": {"text": "safe", "authorization": "Bearer must-not-cross"}}
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(request)

    assert raised.value.code == "invocation_input_invalid"
    assert not assembler.inputs
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_required_structured_output_missing_after_acceptance_becomes_invalid_response() -> (
    None
):
    adapter = _RecordingAdapter(response=RawInvocationOutcome())
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"
    assert len(adapter.calls) == len(runs.runs) == len(results.results) == 1

    await catalog.aclose()


async def test_message_only_success_remains_valid_without_required_structured_output() -> None:
    adapter = _RecordingAdapter(response=RawInvocationOutcome(message="completed"))
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        definition=_definition(output_schema={"type": "object", "properties": {}}),
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "completed"
    assert result.message == "completed"
    assert result.output is None

    await catalog.aclose()


async def test_adapter_that_swallows_cancellation_cannot_extend_absolute_deadline() -> None:
    adapter = _CancellationIgnoringAdapter()
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=0.01,
            )
        ),
    )

    started = time.perf_counter()
    result = await service.invoke(_request(text="within the input limit"))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.07
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_deadline_exceeded"
    await asyncio.wait_for(adapter.finished.wait(), timeout=0.3)

    await catalog.aclose()


async def test_adapter_cannot_extend_deadline_by_mutating_its_envelope_copy() -> None:
    adapter = _DeadlineTamperingAdapter()
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=0.01,
            )
        ),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_deadline_exceeded"
    assert adapter.calls[0].deadline_at > datetime.now(UTC)

    await catalog.aclose()


async def test_runtime_shutdown_drains_a_late_adapter_before_catalog_disposal() -> None:
    adapter = _LifecycleTrackedLateAdapter()
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=0.01,
        )
    )
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        runtime=runtime,
    )

    result = await service.invoke(_request(text="within the input limit"))
    assert result.error is not None
    assert result.error.code == "invocation_deadline_exceeded"
    await asyncio.wait_for(adapter.first_cancelled.wait(), timeout=0.2)

    stopping = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()
    adapter.release.set()
    await asyncio.wait_for(stopping, timeout=0.2)

    await catalog.aclose()


async def test_plan_preflight_rejection_does_not_claim_or_start_the_step() -> None:
    adapter = _RecordingAdapter()
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "contract-plan",
                "tenant_id": "tenant-1",
                "user_id": "operator-1",
                "session_id": "contract-session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "contract-step",
                        "agent_id": "contract-agent",
                        "description": "Run the contract Agent",
                    }
                ],
            }
        ),
        publish=False,
    )
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=32)),
        plan_service=plan_service,
    )
    selection = service.snapshot_runtime.snapshot.select_for_user(
        "contract-agent", _request(text="unused").user
    )
    assert selection is not None

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke_agent(
            agent_id="contract-agent",
            session_id="contract-session",
            user=_request(text="unused").user,
            input={"text": "x" * 128},
            context={"plan_id": plan.plan_id},
            selected_binding=selection,
        )

    restored = await plan_service.get_plan(
        plan.plan_id,
        tenant_id="tenant-1",
        user_id="operator-1",
    )
    assert raised.value.code == "invocation_input_limit_exceeded"
    assert restored is not None
    assert restored.state_version == plan.state_version
    assert restored.steps[0].status == "pending"
    assert plan.plan_id not in plans.execution_claims
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_plan_claim_timeout_does_not_create_a_run_or_dispatch_an_adapter() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "deadline-plan",
                "tenant_id": "tenant-1",
                "user_id": "operator-1",
                "session_id": "contract-session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "deadline-step",
                        "agent_id": "contract-agent",
                        "description": "Run after the claim deadline",
                    }
                ],
            }
        ),
        publish=False,
    )
    original_claim = plan_service.claim_step

    async def late_claim(*args: object, **kwargs: object):
        claimed = await original_claim(*args, **kwargs)
        clock.advance(timedelta(seconds=2))
        return claimed

    plan_service.claim_step = late_claim  # type: ignore[method-assign]
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=1,
            ),
            clock=clock,
        ),
        plan_service=plan_service,
    )
    selection = service.snapshot_runtime.snapshot.select_for_user(
        "contract-agent", _request(text="unused").user
    )
    assert selection is not None

    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await service.invoke_agent(
                agent_id="contract-agent",
                session_id="contract-session",
                user=_request(text="unused").user,
                input={"text": "within the input limit"},
                context={"plan_id": plan.plan_id},
                selected_binding=selection,
            )

        # The claim completed just as the deadline expired. Its deferred
        # compensation must restore the pre-acceptance Plan state instead of
        # leaving a lease that blocks a later legitimate invocation.
        for _ in range(10):
            if plan.plan_id not in plans.execution_claims:
                break
            await asyncio.sleep(0)
        restored = await plan_service.get_plan(
            plan.plan_id,
            tenant_id="tenant-1",
            user_id="operator-1",
        )
        assert restored is not None
        assert restored.steps[0].status == "pending"
        assert plan.plan_id not in plans.execution_claims
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_expiry_after_plan_claim_releases_claim_before_run_acceptance() -> None:
    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    adapter = _RecordingAdapter()
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "post-claim-deadline-plan",
                "tenant_id": "tenant-1",
                "user_id": "operator-1",
                "session_id": "contract-session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "post-claim-deadline-step",
                        "agent_id": "contract-agent",
                        "description": "Expire after a claimed Step",
                    }
                ],
            }
        ),
        publish=False,
    )
    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(
            max_input_bytes=512,
            default_deadline_seconds=1,
        ),
        clock=clock,
    )
    original_attach = runtime.attach_trusted_plan_idempotency

    def expire_after_claim(
        envelope: AgentCallEnvelope,
        invocation: AgentInvocation,
    ) -> AgentCallEnvelope:
        attached = original_attach(envelope, invocation)
        clock.advance(timedelta(seconds=2))
        return attached

    runtime.attach_trusted_plan_idempotency = expire_after_claim  # type: ignore[method-assign]
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=runtime,
        plan_service=plan_service,
    )
    selection = service.snapshot_runtime.snapshot.select_for_user(
        "contract-agent", _request(text="unused").user
    )
    assert selection is not None

    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await service.invoke_agent(
                agent_id="contract-agent",
                session_id="contract-session",
                user=_request(text="unused").user,
                input={"text": "within the input limit"},
                context={"plan_id": plan.plan_id},
                selected_binding=selection,
            )

        for _ in range(10):
            if plan.plan_id not in plans.execution_claims:
                break
            await asyncio.sleep(0)
        restored = await plan_service.get_plan(
            plan.plan_id,
            tenant_id="tenant-1",
            user_id="operator-1",
        )
        assert restored is not None
        assert restored.steps[0].status == "pending"
        assert plan.plan_id not in plans.execution_claims
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_fenced_plan_claim_without_run_restarts_with_its_stable_run_id(
    backend, tmp_path, managed_database
) -> None:
    """A crash after fence commit can resume only the original Run identity."""

    if backend == "memory":
        repository = MemoryPlanRepository()
        recovered_repository = repository
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'fenced-plan-restart.db'}",
        )
        await managed_database.initialize_schema(settings)
        session_factory = await managed_database.session_factory(settings)
        repository = DatabasePlanRepository(session_factory)
        # A new repository object models the restarted worker rather than a
        # convenient in-memory continuation of the original service graph.
        recovered_repository = DatabasePlanRepository(session_factory)
    initial_plans = PlanService(repository)
    plan = await initial_plans.save_plan(
        Plan.model_validate(
            {
                "plan_id": f"fenced-restart-{backend}",
                "tenant_id": "tenant-1",
                "user_id": "operator-1",
                "session_id": "contract-session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "fenced-step",
                        "agent_id": "contract-agent",
                        "description": "Resume the fenced execution once",
                    }
                ],
            }
        ),
        publish=False,
    )
    claimed = await initial_plans.claim_step(
        plan.plan_id,
        "fenced-step",
        tenant_id="tenant-1",
        user_id="operator-1",
    )
    assert claimed is not None
    _, claim_id = claimed
    execution_key = await initial_plans.get_execution_claim_key(plan.plan_id, claim_id=claim_id)
    assert execution_key is not None
    assert await initial_plans.fence_step_claim(
        plan.plan_id,
        "fenced-step",
        tenant_id="tenant-1",
        user_id="operator-1",
        claim_id=claim_id,
    )

    recovered_plans = PlanService(recovered_repository)
    fence = await recovered_plans.get_execution_claim_fence(
        plan.plan_id,
        tenant_id="tenant-1",
        user_id="operator-1",
    )
    assert fence is not None
    assert (fence.step_id, fence.claim_id, fence.execution_key) == (
        "fenced-step",
        claim_id,
        execution_key,
    )

    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
        plan_service=recovered_plans,
    )
    selection = service.snapshot_runtime.snapshot.select_for_user(
        "contract-agent", _request(text="unused").user
    )
    assert selection is not None
    try:
        result = await service.invoke_agent(
            agent_id="contract-agent",
            session_id="contract-session",
            user=_request(text="unused").user,
            input={"text": "within the input limit"},
            context={"plan_id": plan.plan_id},
            selected_binding=selection,
        )

        assert result.status == "completed"
        assert result.run_id == f"run_{execution_key}"
        assert set(runs.runs) == {f"run_{execution_key}"}
        assert len(results.results) == len(adapter.calls) == 1
        for _ in range(20):
            if (
                await recovered_plans.get_execution_claim_fence(
                    plan.plan_id,
                    tenant_id="tenant-1",
                    user_id="operator-1",
                )
                is None
            ):
                break
            await asyncio.sleep(0)
        assert (
            await recovered_plans.get_execution_claim_fence(
                plan.plan_id,
                tenant_id="tenant-1",
                user_id="operator-1",
            )
            is None
        )
    finally:
        await catalog.aclose()


async def test_late_cross_turn_start_ack_never_terminalizes_the_winning_run() -> None:
    """A losing Turn's deadline recovery must not settle a shared stable Run."""

    clock = _Clock(datetime(2030, 1, 1, tzinfo=UTC))
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "cross-turn-fenced-plan",
                "tenant_id": "tenant-1",
                "user_id": "operator-1",
                "session_id": "contract-session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "cross-turn-step",
                        "agent_id": "contract-agent",
                        "description": "Keep one stable Run across Turn contention",
                    }
                ],
            }
        ),
        publish=False,
    )
    claimed = await plan_service.claim_step(
        plan.plan_id,
        "cross-turn-step",
        tenant_id="tenant-1",
        user_id="operator-1",
        publish=False,
    )
    assert claimed is not None
    _, claim_id = claimed
    execution_key = await plan_service.get_execution_claim_key(plan.plan_id, claim_id=claim_id)
    assert execution_key is not None
    assert await plan_service.fence_step_claim(
        plan.plan_id,
        "cross-turn-step",
        tenant_id="tenant-1",
        user_id="operator-1",
        claim_id=claim_id,
    )

    runtime = InvocationRuntime(
        policy=InvocationRuntimePolicy(max_input_bytes=512, default_deadline_seconds=1),
        clock=clock,
    )
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=runtime,
        plan_service=plan_service,
    )
    turns = MemoryTurnRepository()
    turn_service = TurnService(turns)
    outbox = MemoryTurnOutboxRepository()
    canonical = MemoryCanonicalInvocationStore(
        run_repository=runs,
        result_repository=results,
        turn_repository=turns,
        outbox_repository=outbox,
    )
    first = await turn_service.start_turn(
        tenant_id="tenant-1",
        user_id="operator-1",
        session_id="contract-session",
        request_id="cross-turn-winner",
        source="host_chat",
        user_input=TurnUserInput(text="winner"),
    )
    second = await turn_service.start_turn(
        tenant_id="tenant-1",
        user_id="operator-1",
        session_id="contract-session",
        request_id="cross-turn-loser",
        source="host_chat",
        user_input=TurnUserInput(text="loser"),
    )
    winning_run, _, _, created = await canonical.start_run(
        AgentRun(
            run_id=f"run_{execution_key}",
            request_id=first.turn.request_id,
            session_id=first.turn.session_id,
            agent_id="contract-agent",
            user_id="operator-1",
            tenant_id="tenant-1",
            plan_id=plan.plan_id,
            step_id="cross-turn-step",
            status="running",
            invoker_type="contract_adapter",
        )
    )
    assert created is True

    # The losing request begins its fenced recovery before it observes the
    # winner's commit. Its next canonical start sees the stable-id conflict,
    # then receives the acknowledgement after its own deadline.
    original_get_run = runs.get_run
    hide_winner_once = True

    async def get_run_with_stale_first_read(run_id: str):
        nonlocal hide_winner_once
        if run_id == winning_run.run_id and hide_winner_once:
            hide_winner_once = False
            return None
        return await original_get_run(run_id)

    runs.get_run = get_run_with_stale_first_read  # type: ignore[method-assign]
    service.canonical_invocation_store = _DeadlineAdvancingCanonicalStore(
        delegate=canonical,
        clock=clock,
        advance_by=timedelta(seconds=2),
    )
    selection = service.snapshot_runtime.snapshot.select_for_user(
        "contract-agent", _request(text="unused").user
    )
    assert selection is not None

    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await service.invoke_agent(
                agent_id="contract-agent",
                session_id="contract-session",
                user=_request(text="unused").user,
                input={"text": "do not settle another Turn's Run"},
                request_id=second.turn.request_id,
                context={
                    "plan_id": plan.plan_id,
                    "_canonical_turn_managed": True,
                    "_canonical_response_text": "loser response",
                },
                selected_binding=selection,
            )

        for _ in range(20):
            if not runtime._durable_start_reconciliation_tasks:
                break
            await asyncio.sleep(0)
        stored_winner = await original_get_run(winning_run.run_id)
        stored_loser_turn = await turns.get(
            second.turn.turn_id,
            tenant_id="tenant-1",
            user_id="operator-1",
        )
        assert stored_winner is not None and stored_winner.status == "running"
        assert stored_loser_turn is not None and stored_loser_turn.status.value == "pending"
        assert results.results == []
        assert adapter.calls == []
    finally:
        await catalog.aclose()


async def test_cancelled_plan_claim_publication_is_compensated_before_run_acceptance() -> None:
    adapter = _RecordingAdapter()
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "claim-publication-plan",
                "tenant_id": "tenant-1",
                "user_id": "operator-1",
                "session_id": "contract-session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "claim-publication-step",
                        "agent_id": "contract-agent",
                        "description": "Block publication after Claim persistence",
                    }
                ],
            }
        ),
        publish=False,
    )
    publication_started = asyncio.Event()

    async def block_claim_publication(*_args: object, **_kwargs: object) -> None:
        publication_started.set()
        await asyncio.Future()

    plan_service._publish = block_claim_publication  # type: ignore[method-assign]
    catalog, service, runs, results = await _service(
        adapter=adapter,
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(
                max_input_bytes=512,
                default_deadline_seconds=0.01,
            )
        ),
        plan_service=plan_service,
    )
    selection = service.snapshot_runtime.snapshot.select_for_user(
        "contract-agent", _request(text="unused").user
    )
    assert selection is not None

    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await service.invoke_agent(
                agent_id="contract-agent",
                session_id="contract-session",
                user=_request(text="unused").user,
                input={"text": "within the input limit"},
                context={"plan_id": plan.plan_id},
                selected_binding=selection,
            )

        assert publication_started.is_set()
        for _ in range(10):
            if plan.plan_id not in plans.execution_claims:
                break
            await asyncio.sleep(0)
        restored = await plan_service.get_plan(
            plan.plan_id,
            tenant_id="tenant-1",
            user_id="operator-1",
        )
        assert restored is not None
        assert restored.steps[0].status == "pending"
        assert plan.plan_id not in plans.execution_claims
        assert adapter.calls == []
        assert runs.runs == {}
        assert results.results == []
    finally:
        await catalog.aclose()


async def test_definition_input_limit_can_only_tighten_the_deployment_limit() -> None:
    adapter = _RecordingAdapter()
    catalog, service, runs, results = await _service(
        adapter=adapter,
        definition=_definition(limits={"max_input_bytes": 32}),
        runtime=InvocationRuntime(policy=InvocationRuntimePolicy(max_input_bytes=512)),
    )

    with pytest.raises(InvocationPreflightRejectedError) as raised:
        await service.invoke(_request(text="x" * 128))

    assert raised.value.code == "invocation_input_limit_exceeded"
    assert not adapter.calls
    assert not runs.runs
    assert not results.results

    await catalog.aclose()


async def test_definition_limit_cannot_relax_a_deployment_output_limit() -> None:
    adapter = _RecordingAdapter(
        response=RawInvocationOutcome(message="x" * 64, output={"summary": "completed"})
    )
    catalog, service, _runs, _results = await _service(
        adapter=adapter,
        definition=_definition(limits={"max_message_chars": 100_000}),
        runtime=InvocationRuntime(
            policy=InvocationRuntimePolicy(max_input_bytes=512, max_message_chars=16)
        ),
    )

    result = await service.invoke(_request(text="within the input limit"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invocation_invalid_response"

    await catalog.aclose()
