"""Process-scoped Invocation Runtime and its narrow Adapter contract.

Adapters receive a resolved, deployment-owned binding and a minimal call
envelope.  They never receive an Agent Definition or construct public Run
identity; the Runtime owns that projection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from types import MappingProxyType

from pydantic import Field

from app.core.errors import InvocationError
from app.schemas.common import ArtifactRef, JsonDict, StrictBaseModel
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class InvocationPrincipalProjection(StrictBaseModel):
    """The initial least-privilege principal projection for Runtime Adapters."""

    subject: str = Field(min_length=1)
    tenant_id: str | None = None


class AgentCallEnvelope(StrictBaseModel):
    """Input made available to one Runtime Adapter execution.

    This is deliberately distinct from ``AgentInvocation``.  The latter stays
    inside Core orchestration and persistence; the Adapter only receives facts
    required to perform a single accepted call.
    """

    execution_id: str = Field(min_length=1)
    request_id: str | None = None
    session_id: str = Field(min_length=1)
    principal: InvocationPrincipalProjection
    input: JsonDict = Field(default_factory=dict)
    idempotency_key: str | None = None


class RawInvocationOutcome(StrictBaseModel):
    """Closed, identity-free successful outcome returned by a Runtime Adapter.

    Failure categorisation and post-acceptance convergence are introduced by
    the following Invocation Runtime slice.  This initial tracer-bullet
    contract intentionally has no Run, Agent, status, or HTTP error fields.
    """

    message: str = ""
    output: JsonDict | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    usage: JsonDict = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeAdapterBinding:
    """Safe, already-resolved Adapter configuration for one invocation."""

    adapter_key: str
    config: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))


RuntimeAdapterExecutor = Callable[
    [RuntimeAdapterBinding, AgentCallEnvelope],
    Awaitable[RawInvocationOutcome],
]


@dataclass(frozen=True, slots=True)
class RuntimeAdapterExecution:
    """One Adapter callable captured during Binding Resolution.

    The callable is captured exactly once so the execution path does not look
    up a mutable Adapter method after preflight has succeeded.
    """

    binding: RuntimeAdapterBinding
    execute: RuntimeAdapterExecutor

    async def invoke(self, envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        candidate = self.execute(self.binding, envelope)
        if not isawaitable(candidate):
            raise InvocationError("Runtime Adapter execution must be async")
        outcome = await candidate
        if not isinstance(outcome, RawInvocationOutcome):
            raise InvocationError("Runtime Adapter returned an invalid raw outcome")
        return outcome


class InvocationRuntime:
    """Process-owned Runtime that projects Adapter outcomes into public results."""

    async def execute(
        self,
        *,
        execution: RuntimeAdapterExecution,
        invocation: AgentInvocation,
        agent_id: str,
    ) -> AgentInvocationResult:
        envelope = _build_call_envelope(invocation)
        outcome = await execution.invoke(envelope)
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=agent_id,
            status="completed",
            message=outcome.message,
            output=outcome.output,
            artifact_refs=outcome.artifact_refs,
            usage=dict(outcome.usage),
        )


def _build_call_envelope(invocation: AgentInvocation) -> AgentCallEnvelope:
    idempotency_key = _canonical_plan_idempotency_key(invocation)
    return AgentCallEnvelope(
        execution_id=invocation.run_id,
        request_id=invocation.request_id,
        session_id=invocation.session_id,
        principal=InvocationPrincipalProjection(
            subject=invocation.user.id,
            tenant_id=invocation.user.tenant_id,
        ),
        input={
            key: value
            for key, value in invocation.input.items()
            if key not in {"memory_context", "knowledge_context"}
        },
        idempotency_key=idempotency_key,
    )


def _canonical_plan_idempotency_key(invocation: AgentInvocation) -> str | None:
    """Project a key only after Core has established a canonical Plan claim.

    Runtime Adapter caches are deliberately non-authoritative.  A client or
    an ad-hoc Direct Invoke cannot turn an arbitrary context field into a
    second idempotency state machine; only the existing Plan claim flow emits
    the complete trusted tuple.
    """

    context = invocation.context
    required_keys = (
        "plan_id",
        "step_id",
        "plan_execution_claim_id",
        "plan_execution_idempotency_key",
    )
    values = [context.get(key) for key in required_keys]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values[-1]
