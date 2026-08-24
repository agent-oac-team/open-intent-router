import asyncio
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import InvocationError
from app.main import create_app
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
    RawInvocationOutcome,
    RuntimeAdapterBinding,
    RuntimeAdapterExecution,
)
from app.runtime.local_function import (
    LocalFunctionRuntimeAdapter,
    LocalFunctionRuntimeRegistry,
    local_function_runtime_descriptor,
)
from app.schemas.agent_context import AgentRuntimeContext
from app.schemas.agents import AgentDefinitionV2
from app.schemas.invocation import AgentInvocation, InvokeRequest
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime
from tests.support.runtime_adapter_conformance import (
    RuntimeAdapterConformanceCase,
    assert_runtime_adapter_conforms,
)


@dataclass
class _RecordingAdapter:
    calls: list[tuple[RuntimeAdapterBinding, AgentCallEnvelope]] = field(default_factory=list)

    async def execute(
        self,
        binding: RuntimeAdapterBinding,
        _connector: object,
        envelope: AgentCallEnvelope,
    ) -> RawInvocationOutcome:
        self.calls.append((binding, envelope))
        return RawInvocationOutcome(
            message="Local work completed.",
            output={"summary": "completed"},
        )


class _PassthroughAgentContextAssembler:
    """Makes InvocationService inject its normal context fields for this boundary test."""

    async def assemble(self, **_kwargs: object) -> AgentRuntimeContext:
        return AgentRuntimeContext()


async def _noop(_adapter: object) -> None:
    return None


async def _healthy(_adapter: object) -> bool:
    return True


def _definition(
    *,
    adapter_key: str,
    function: str,
    agent_id: str = "conformance-agent",
) -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": agent_id,
            "name": "Conformance Agent",
            "description": "Tests the Runtime Adapter contract.",
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
                "config": {"function": function},
            },
        }
    )


def _invocation(*, run_id: str, agent_id: str) -> AgentInvocation:
    return AgentInvocation.model_validate(
        {
            "run_id": run_id,
            "session_id": "conformance-session",
            "agent_id": agent_id,
            "user": {"id": "operator-1", "roles": ["operator"]},
            "input": {"text": "conformance input"},
        }
    )


async def test_runtime_binds_public_identity_without_exposing_definition_to_adapter() -> None:
    adapter = _RecordingAdapter()
    runtime = InvocationRuntime()
    invocation = AgentInvocation.model_validate(
        {
            "run_id": "run-runtime-1",
            "request_id": "request-runtime-1",
            "session_id": "session-runtime-1",
            "agent_id": "must-not-reach-adapter",
            "user": {
                "id": "operator-1",
                "roles": ["operator"],
                "attributes": {
                    "tenant_id": "tenant-1",
                    "token": "must-not-reach-adapter",
                },
            },
            "input": {
                "text": "run local function",
                "declared": "allowed by the input schema",
                "headers": {"authorization": "Bearer must-not-reach-adapter"},
                "memory_context": {"items": [{"content": "memory-secret"}]},
                "knowledge_context": {"items": [{"content": "knowledge-secret"}]},
                "definition": {"connector_ref": "must-not-reach-adapter"},
            },
            "artifact_refs": [
                {
                    "artifact_id": "artifact-runtime-1",
                    "type": "document",
                    "uri": "artifact://runtime-1",
                    "metadata": {"size_bytes": 42},
                }
            ],
            "memory_context": {
                "status": "ok",
                "items": [
                    {
                        "memory_id": "memory-runtime-1",
                        "scope": "stable_fact",
                        "content": "memory-secret",
                    }
                ],
            },
            "knowledge_context": {
                "status": "ok",
                "items": [
                    {
                        "item_id": "knowledge-runtime-1",
                        "source_id": "source-runtime-1",
                        "content": "knowledge-secret",
                    }
                ],
            },
            "context": {
                "plan_id": "plan-runtime-1",
                "step_id": "step-runtime-1",
                "plan_execution_claim_id": "claim-runtime-1",
                "plan_execution_idempotency_key": "plan-execution-1",
                "route_reason": "must-not-reach-adapter",
                "trace_id": "must-not-reach-adapter",
            },
        }
    )

    result = await runtime.execute(
        execution=RuntimeAdapterExecution(
            binding=RuntimeAdapterBinding(
                adapter_key="local_function",
                config={"function": "echo"},
            ),
            execute=adapter.execute,
        ),
        invocation=invocation,
        agent_id="bound-agent",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "declared": {"type": "string"},
                "definition": {"type": "object"},
            },
        },
    )

    assert result.model_dump() == {
        "run_id": "run-runtime-1",
        "agent_id": "bound-agent",
        "status": "completed",
        "message": "Local work completed.",
        "output": {"summary": "completed"},
        "artifact_refs": [],
        "usage": {},
        "error": None,
    }
    assert len(adapter.calls) == 1
    binding, envelope = adapter.calls[0]
    assert binding.adapter_key == "local_function"
    assert dict(binding.config) == {"function": "echo"}
    envelope_data = envelope.model_dump(exclude_none=True)
    assert envelope_data.pop("deadline_at") == envelope.deadline_at
    assert envelope_data == {
        "execution_id": "run-runtime-1",
        "request_id": "request-runtime-1",
        "session_id": "session-runtime-1",
        "principal": {"subject": "operator-1", "tenant_id": "tenant-1"},
        "input": {
            "text": "run local function",
            "declared": "allowed by the input schema",
        },
        "context": {
            "memory": [{"memory_id": "memory-runtime-1", "scope": "stable_fact"}],
            "knowledge": [
                {
                    "item_id": "knowledge-runtime-1",
                    "source_id": "source-runtime-1",
                }
            ],
        },
        "artifact_refs": [
            {
                "artifact_id": "artifact-runtime-1",
                "type": "document",
                "uri": "artifact://runtime-1",
                "metadata": {"size_bytes": 42},
            }
        ],
        "idempotency_key": "plan-execution-1",
    }
    assert envelope.deadline_at is not None
    assert not hasattr(envelope, "agent_id")
    assert "must-not-reach-adapter" not in envelope.model_dump_json()
    assert "memory-secret" not in envelope.model_dump_json()
    assert "knowledge-secret" not in envelope.model_dump_json()


def test_raw_outcome_is_identity_free_and_closed() -> None:
    with pytest.raises(ValidationError):
        RawInvocationOutcome.model_validate(
            {
                "run_id": "adapter-controlled-run",
                "agent_id": "adapter-controlled-agent",
                "status": "completed",
            }
        )


async def test_runtime_ignores_an_incomplete_caller_idempotency_context() -> None:
    adapter = _RecordingAdapter()
    invocation = AgentInvocation.model_validate(
        {
            "run_id": "run-untrusted-key",
            "session_id": "session-untrusted-key",
            "agent_id": "not-for-adapter",
            "user": {"id": "operator-1"},
            "input": {"text": "direct invocation"},
            "context": {"plan_execution_idempotency_key": "caller-controlled-key"},
        }
    )

    await InvocationRuntime().execute(
        execution=RuntimeAdapterExecution(
            binding=RuntimeAdapterBinding(
                adapter_key="local_function",
                config={"function": "echo"},
            ),
            execute=adapter.execute,
        ),
        invocation=invocation,
        agent_id="bound-agent",
    )

    assert adapter.calls[0][1].idempotency_key is None


async def test_conformance_harness_exercises_controlled_and_local_runtime_adapters() -> None:
    controlled = _RecordingAdapter()
    controlled_descriptor = RuntimeAdapterDescriptor(
        key="controlled",
        contract_version="controlled-contract-v1",
        implementation_version="controlled-implementation-v1",
        config_schema={
            "type": "object",
            "required": ["function"],
            "properties": {"function": {"const": "echo"}},
        },
        capability=RuntimeAdapterCapability(
            invocation=True,
        ),
        factory=lambda _context: controlled,
        health_check=_healthy,
        lifecycle=RuntimeAdapterLifecycle(activate=_noop, dispose=_noop),
    )
    local_registry = LocalFunctionRuntimeRegistry()

    async def local_echo(_envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        return RawInvocationOutcome(output={"summary": "local"})

    local_registry.register("echo", local_echo)

    def assert_local_disposed(adapter: object) -> None:
        assert isinstance(adapter, LocalFunctionRuntimeAdapter)
        assert adapter.closed is True

    cases = (
        RuntimeAdapterConformanceCase(
            descriptor=controlled_descriptor,
            definition=_definition(adapter_key="controlled", function="echo"),
            request=InvokeRequest.model_validate(
                {
                    "session_id": "conformance-session",
                    "agent_id": "conformance-agent",
                    "user": {"id": "operator-1", "roles": ["operator"]},
                    "input": {"text": "controlled conformance input"},
                }
            ),
        ),
        RuntimeAdapterConformanceCase(
            descriptor=local_function_runtime_descriptor(local_registry),
            definition=_definition(adapter_key="local_function", function="echo"),
            request=InvokeRequest.model_validate(
                {
                    "session_id": "conformance-session",
                    "agent_id": "conformance-agent",
                    "user": {"id": "operator-1", "roles": ["operator"]},
                    "input": {"text": "local conformance input"},
                }
            ),
            assert_disposed=assert_local_disposed,
        ),
    )

    controlled_result = await assert_runtime_adapter_conforms(cases[0])
    local_result = await assert_runtime_adapter_conforms(cases[1])

    assert controlled_result.output == {"summary": "completed"}
    assert local_result.output == {"summary": "local"}
    assert len(controlled.calls) == 1
    assert local_registry.frozen is True


async def test_local_function_adapter_runs_through_one_process_runtime_and_disposes() -> None:
    calls: list[AgentCallEnvelope] = []
    registry = LocalFunctionRuntimeRegistry()

    async def summarize(envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        calls.append(envelope)
        return RawInvocationOutcome(
            message="summarized",
            output={"summary": str(envelope.input["text"])},
        )

    registry.register("summarize", summarize)
    catalog = await RuntimeCatalog.activate(
        [local_function_runtime_descriptor(registry)],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    adapter = catalog.get("local_function")
    assert isinstance(adapter, LocalFunctionRuntimeAdapter)
    snapshot_runtime = RegistrySnapshotRuntime(RegistrySnapshotBuilder(catalog))
    snapshot_runtime.load(
        [
            AgentDefinitionV2.model_validate(
                {
                    "schema_version": "oir-agent-v2",
                    "agent_id": "local-summarizer",
                    "name": "Local Summarizer",
                    "description": "Runs a trusted local function.",
                    "revision": 1,
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
                    "handling": {
                        "kind": "invocation",
                        "adapter_key": "local_function",
                        "config": {"function": "summarize"},
                    },
                }
            )
        ],
        source="test",
    )
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = InvocationService(
        registry=object(),
        run_repository=runs,
        result_repository=results,
        snapshot_runtime=snapshot_runtime,
        binding_resolver=BindingResolver(catalog),
        invocation_runtime=InvocationRuntime(),
        agent_context_service=_PassthroughAgentContextAssembler(),
    )
    request = InvokeRequest.model_validate(
        {
            "session_id": "local-session",
            "agent_id": "local-summarizer",
            "user": {
                "id": "operator-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1", "token": "not-for-adapter"},
            },
            "input": {"text": "a short paragraph"},
            "memory_context": {
                "status": "ok",
                "items": [
                    {
                        "memory_id": "memory-1",
                        "scope": "task_memory",
                        "content": "memory-secret",
                    }
                ],
            },
            "knowledge_context": {
                "status": "ok",
                "items": [
                    {
                        "item_id": "knowledge-1",
                        "source_id": "source-1",
                        "content": "knowledge-secret",
                    }
                ],
            },
        }
    )

    first = await service.invoke(request)
    second = await service.invoke(request)

    assert first.status == second.status == "completed"
    assert first.run_id != second.run_id
    assert first.output == second.output == {"summary": "a short paragraph"}
    assert [call.execution_id for call in calls] == [first.run_id, second.run_id]
    assert all(not hasattr(call, "agent_id") for call in calls)
    assert all("not-for-adapter" not in call.model_dump_json() for call in calls)
    assert all("memory-secret" not in call.model_dump_json() for call in calls)
    assert all("knowledge-secret" not in call.model_dump_json() for call in calls)
    assert len(runs.runs) == len(results.results) == 2
    assert registry.frozen is True

    await catalog.aclose()

    assert adapter.closed is True
    assert adapter.completed_count == 0


async def test_local_function_adapter_reuses_existing_plan_execution_key_across_runs() -> None:
    registry = LocalFunctionRuntimeRegistry()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def once_only(_envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return RawInvocationOutcome(output={"summary": "one execution"})

    registry.register("once_only", once_only)
    catalog = await RuntimeCatalog.activate(
        [local_function_runtime_descriptor(registry)],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    adapter = catalog.get("local_function")
    assert isinstance(adapter, LocalFunctionRuntimeAdapter)
    execution = RuntimeAdapterExecution(
        binding=RuntimeAdapterBinding(
            adapter_key="local_function",
            config={"function": "once_only"},
        ),
        execute=adapter.execute,
    )
    runtime = InvocationRuntime()

    def invocation(run_id: str) -> AgentInvocation:
        return AgentInvocation.model_validate(
            {
                "run_id": run_id,
                "session_id": "plan-session",
                "agent_id": "not-for-adapter",
                "user": {"id": "operator-1", "attributes": {"tenant_id": "tenant-1"}},
                "input": {"text": "once"},
                "context": {
                    "plan_id": "plan-local-1",
                    "step_id": "step-local-1",
                    "plan_execution_claim_id": "claim-local-1",
                    "plan_execution_idempotency_key": "plan-step-execution-1",
                },
            }
        )

    first_task = asyncio.create_task(
        runtime.execute(
            execution=execution,
            invocation=invocation("run-plan-1"),
            agent_id="bound-agent",
        )
    )
    await started.wait()
    second_task = asyncio.create_task(
        runtime.execute(
            execution=execution,
            invocation=invocation("run-plan-2"),
            agent_id="bound-agent",
        )
    )
    await asyncio.sleep(0)

    assert calls == 1
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    third = await runtime.execute(
        execution=execution,
        invocation=invocation("run-plan-3"),
        agent_id="bound-agent",
    )

    assert [first.run_id, second.run_id] == ["run-plan-1", "run-plan-2"]
    assert first.output == second.output == {"summary": "one execution"}
    assert third.run_id == "run-plan-3"
    assert third.output == {"summary": "one execution"}
    assert calls == 1
    assert adapter.completed_count == 1

    await catalog.aclose()


async def test_local_function_adapter_does_not_cache_a_failed_plan_execution() -> None:
    registry = LocalFunctionRuntimeRegistry()
    calls = 0

    async def flaky(_envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InvocationError("controlled local failure")
        return RawInvocationOutcome(output={"summary": "recovered"})

    registry.register("flaky", flaky)
    catalog = await RuntimeCatalog.activate(
        [local_function_runtime_descriptor(registry)],
        RuntimeAdapterContext(settings=Settings(storage_backend="memory")),
        shutdown_timeout_seconds=1,
    )
    adapter = catalog.get("local_function")
    assert isinstance(adapter, LocalFunctionRuntimeAdapter)
    execution = RuntimeAdapterExecution(
        binding=RuntimeAdapterBinding(
            adapter_key="local_function",
            config={"function": "flaky"},
        ),
        execute=adapter.execute,
    )
    invocation = AgentInvocation.model_validate(
        {
            "run_id": "run-flaky",
            "session_id": "plan-session",
            "agent_id": "not-for-adapter",
            "user": {"id": "operator-1", "attributes": {"tenant_id": "tenant-1"}},
            "input": {"text": "retry"},
            "context": {
                "plan_id": "plan-local-2",
                "step_id": "step-local-2",
                "plan_execution_claim_id": "claim-local-2",
                "plan_execution_idempotency_key": "plan-step-flaky-2",
            },
        }
    )
    runtime = InvocationRuntime()

    failed = await runtime.execute(
        execution=execution,
        invocation=invocation,
        agent_id="bound-agent",
    )
    recovered = await runtime.execute(
        execution=execution,
        invocation=invocation.model_copy(update={"run_id": "run-flaky-retry"}),
        agent_id="bound-agent",
    )

    assert calls == 2
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.code == "invocation_remote_failure"
    assert recovered.run_id == "run-flaky-retry"
    assert recovered.output == {"summary": "recovered"}
    assert adapter.completed_count == 1

    await catalog.aclose()


def test_local_function_descriptor_creates_a_fresh_adapter_per_app_lifespan(tmp_path) -> None:
    registry = LocalFunctionRuntimeRegistry()

    async def echo(_envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        return RawInvocationOutcome(output={"summary": "echo"})

    registry.register("echo", echo)
    app = create_app(
        settings=Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'local-runtime.db'}",
            registry_backend="file",
            memory_mode="off",
        ),
        runtime_descriptors=(local_function_runtime_descriptor(registry),),
    )

    with TestClient(app):
        first_catalog = app.state.runtime_catalog_runtime.catalog
        assert first_catalog is not None
        first_adapter = first_catalog.get("local_function")
        assert isinstance(first_adapter, LocalFunctionRuntimeAdapter)
        assert first_adapter.closed is False

    assert first_adapter.closed is True

    with TestClient(app):
        second_catalog = app.state.runtime_catalog_runtime.catalog
        assert second_catalog is not None
        second_adapter = second_catalog.get("local_function")
        assert isinstance(second_adapter, LocalFunctionRuntimeAdapter)
        assert second_adapter.closed is False

    assert second_adapter is not first_adapter
    assert second_adapter.closed is True
