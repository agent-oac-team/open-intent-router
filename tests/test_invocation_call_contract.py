import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.errors import InvocationPreflightRejectedError
from app.repositories.memory import (
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
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
from app.schemas.invocation import InvokeRequest
from app.schemas.plans import Plan
from app.services.agent_context_service import KnowledgeRequirementError
from app.services.binding_resolution import BindingResolver
from app.services.invocation_service import InvocationService
from app.services.plan_service import PlanService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime


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
    assert len(adapter.calls) == 1
    assert adapter.calls[0].deadline_at.tzinfo is not None
    assert len(runs.runs) == len(results.results) == 1

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
