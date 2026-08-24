"""Process-scoped Invocation Runtime and its narrow Adapter contract.

Adapters receive a resolved, deployment-owned binding and a minimal call
envelope.  They never receive an Agent Definition or construct public Run
identity; the Runtime owns that projection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from types import MappingProxyType
from typing import Literal

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema
from pydantic import Field, ValidationError, model_validator

from app.schemas.common import ArtifactRef, ErrorDetail, JsonDict, StrictBaseModel
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


InvocationFailureCategory = Literal[
    "unavailable",
    "deadline_exceeded",
    "rejected",
    "remote_failure",
    "invalid_response",
]
_FAILURE_CODE_BY_CATEGORY: Mapping[InvocationFailureCategory, str] = {
    "unavailable": "invocation_unavailable",
    "deadline_exceeded": "invocation_deadline_exceeded",
    "rejected": "invocation_rejected",
    "remote_failure": "invocation_remote_failure",
    "invalid_response": "invocation_invalid_response",
}
_PUBLIC_FAILURE_MESSAGE = "Agent invocation could not be completed."


class RawInvocationFailure(StrictBaseModel):
    """Closed, safe failure facts an Adapter may return after acceptance.

    The narrow category/code pairing intentionally rejects Adapter-specific
    exception text, remote payloads, URLs, credentials, and diagnostics.  A
    future Runtime slice may add an explicitly reviewed stable code without
    reopening this result contract to arbitrary strings.
    """

    category: InvocationFailureCategory
    code: str = Field(min_length=1, max_length=64)
    retryable: bool

    @classmethod
    def for_category(
        cls,
        category: InvocationFailureCategory,
        *,
        retryable: bool,
    ) -> RawInvocationFailure:
        """Build a closed failure fact without duplicating safe-code mappings."""

        return cls(
            category=category,
            code=_FAILURE_CODE_BY_CATEGORY[category],
            retryable=retryable,
        )

    @model_validator(mode="after")
    def validate_safe_code(self) -> RawInvocationFailure:
        if self.code != _FAILURE_CODE_BY_CATEGORY[self.category]:
            raise ValueError("Invocation failure code is not valid for its category")
        return self


class RawInvocationUsage(StrictBaseModel):
    """Small, numeric-only usage facts that may cross the Adapter boundary."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    def public_values(self) -> JsonDict:
        return self.model_dump(mode="json", exclude_none=True)


class RawInvocationOutcome(StrictBaseModel):
    """Closed, identity-free Runtime Adapter outcome.

    A successful outcome carries only public result content.  A failed outcome
    carries exactly one ``RawInvocationFailure`` and no Adapter-supplied text
    or output, keeping all public Run/Result projection in the Runtime.
    """

    message: str = ""
    output: JsonDict | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    usage: RawInvocationUsage = Field(default_factory=RawInvocationUsage)
    failure: RawInvocationFailure | None = None

    @model_validator(mode="after")
    def validate_failure_branch(self) -> RawInvocationOutcome:
        if self.failure is None:
            return self
        if (
            self.message
            or self.output is not None
            or self.artifact_refs
            or self.usage.public_values()
        ):
            raise ValueError("Failed Invocation outcomes cannot carry Adapter result content")
        return self


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

_RAW_OUTCOME_FIELDS = (
    "message",
    "output",
    "artifact_refs",
    "usage",
    "failure",
)


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
            raise _InvalidRuntimeAdapterOutcome
        outcome = await candidate
        if not isinstance(outcome, RawInvocationOutcome):
            raise _InvalidRuntimeAdapterOutcome
        try:
            # Never serialize an Adapter-owned model before revalidating it:
            # Pydantic's serializer warning can itself render malformed values
            # (including a credential) to stderr.  Read only declared fields
            # and validate the resulting untrusted values without logging them.
            values = object.__getattribute__(outcome, "__dict__")
            return RawInvocationOutcome.model_validate(
                {field: values[field] for field in _RAW_OUTCOME_FIELDS if field in values}
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise _InvalidRuntimeAdapterOutcome from None


class InvocationRuntime:
    """Process-owned Runtime that projects Adapter outcomes into public results."""

    async def execute(
        self,
        *,
        execution: RuntimeAdapterExecution,
        invocation: AgentInvocation,
        agent_id: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> AgentInvocationResult:
        envelope = _build_call_envelope(invocation)
        try:
            outcome = await execution.invoke(envelope)
        except asyncio.CancelledError as exc:
            # An accepted execution must never leave its Run in ``running``.
            # This is a safe failure projection, not a claim that a remote
            # side effect was cancelled; later control semantics add a more
            # precise completion-certainty contract.
            return self.project_exception(
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
            )
        except TimeoutError as exc:
            return self.project_exception(
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
            )
        except _InvalidRuntimeAdapterOutcome as exc:
            return self.project_exception(
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
            )
        except Exception as exc:
            return self.project_exception(
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
            )
        if outcome.failure is not None:
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=outcome.failure,
            )
        result = AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=agent_id,
            status="completed",
            message=outcome.message,
            output=outcome.output,
            artifact_refs=outcome.artifact_refs,
            usage=outcome.usage.public_values(),
        )
        return self._validate_success_output(
            result,
            output_schema,
            invocation=invocation,
            agent_id=agent_id,
        )

    def project_exception(
        self,
        *,
        invocation: AgentInvocation,
        agent_id: str,
        exc: BaseException,
    ) -> AgentInvocationResult:
        """Map an Adapter exception without trusting its message or details."""

        if isinstance(exc, TimeoutError):
            failure = RawInvocationFailure.for_category(
                "deadline_exceeded",
                retryable=False,
            )
        elif isinstance(exc, _InvalidRuntimeAdapterOutcome):
            failure = RawInvocationFailure.for_category(
                "invalid_response",
                retryable=False,
            )
        else:
            failure = RawInvocationFailure.for_category(
                "remote_failure",
                retryable=False,
            )
        return self.project_failure(
            invocation=invocation,
            agent_id=agent_id,
            failure=failure,
        )

    def project_failure(
        self,
        *,
        invocation: AgentInvocation,
        agent_id: str,
        failure: RawInvocationFailure,
    ) -> AgentInvocationResult:
        """Create the only public projection for an accepted Adapter failure."""

        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=agent_id,
            status="failed",
            message=_PUBLIC_FAILURE_MESSAGE,
            error=ErrorDetail(
                code=failure.code,
                message=_PUBLIC_FAILURE_MESSAGE,
                details={
                    "category": failure.category,
                    "retryable": failure.retryable,
                },
            ),
        )

    def _validate_success_output(
        self,
        result: AgentInvocationResult,
        output_schema: Mapping[str, object] | None,
        *,
        invocation: AgentInvocation,
        agent_id: str,
    ) -> AgentInvocationResult:
        if result.output is None or output_schema is None or not output_schema.get("properties"):
            return result
        try:
            validate_json_schema(instance=result.output, schema=dict(output_schema))
        except JsonSchemaValidationError:
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "invalid_response",
                    retryable=False,
                ),
            )
        return result


class _InvalidRuntimeAdapterOutcome(Exception):
    """Internal marker; its details never cross the Runtime boundary."""


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
