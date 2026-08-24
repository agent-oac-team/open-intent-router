"""Process-scoped Invocation Runtime and its narrow Adapter contract.

Adapters receive a resolved, deployment-owned binding and a minimal call
envelope.  They never receive an Agent Definition or construct public Run
identity; the Runtime owns that projection.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from math import isfinite
from types import MappingProxyType
from typing import Literal, TypeVar, cast
from urllib.parse import unquote, urlsplit

from jsonschema import SchemaError as JsonSchemaSchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema
from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from app.application import ResolvedConnector
from app.core.errors import InvocationDeadlineExceededError, InvocationPreflightRejectedError
from app.schemas.agents import (
    INVOCATION_PRINCIPAL_CLAIMS,
    InvocationLimits,
    InvocationPrincipalClaim,
    is_safe_invocation_principal_attribute_key,
    is_safe_invocation_reference,
)
from app.schemas.common import ArtifactRef, ErrorDetail, JsonDict, StrictBaseModel
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class InvocationPrincipalProjection(StrictBaseModel):
    """The initial least-privilege principal projection for Runtime Adapters."""

    subject: str = Field(min_length=1)
    tenant_id: str | None = None
    roles: list[str] | None = None
    groups: list[str] | None = None
    entitlements: list[str] | None = None
    attributes: JsonDict | None = None


class InvocationMemoryContextReference(StrictBaseModel):
    """A content-free Memory fact approved for one Adapter call."""

    memory_id: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=64)


class InvocationKnowledgeContextReference(StrictBaseModel):
    """A content-free Knowledge fact approved for one Adapter call."""

    item_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)


class InvocationContextProjection(StrictBaseModel):
    """The governed Context facts, never raw Context bodies or Trace metadata."""

    memory: list[InvocationMemoryContextReference] = Field(default_factory=list)
    knowledge: list[InvocationKnowledgeContextReference] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InvocationRuntimePolicy:
    """Deployment hard limits owned by the process-scoped Invocation Runtime."""

    max_input_bytes: int = 32_768
    max_context_bytes: int = 16_384
    max_message_chars: int = 4_000
    max_output_bytes: int = 32_768
    max_artifact_count: int = 16
    max_artifact_metadata_bytes: int = 2_048
    allowed_principal_claims: frozenset[InvocationPrincipalClaim] = frozenset()
    allowed_principal_attribute_keys: frozenset[str] = frozenset()
    default_deadline_seconds: float = 30.0

    @classmethod
    def from_settings(cls, settings: object) -> InvocationRuntimePolicy:
        """Build the process policy from validated deployment Settings.

        The Runtime intentionally receives a value object instead of reaching
        into environment variables while serving a call. Unknown claims and
        non-logical attribute names are ignored: deployment configuration can
        only grant the small, reviewed projection vocabulary.
        """

        return cls(
            max_input_bytes=int(getattr(settings, "invocation_max_input_bytes", 32_768)),
            max_context_bytes=int(getattr(settings, "invocation_max_context_bytes", 16_384)),
            max_message_chars=int(getattr(settings, "invocation_max_message_chars", 4_000)),
            max_output_bytes=int(getattr(settings, "invocation_max_output_bytes", 32_768)),
            max_artifact_count=int(getattr(settings, "invocation_max_artifact_count", 16)),
            max_artifact_metadata_bytes=int(
                getattr(settings, "invocation_max_artifact_metadata_bytes", 2_048)
            ),
            allowed_principal_claims=frozenset(
                claim
                for claim in _comma_separated_values(
                    getattr(settings, "invocation_allowed_principal_claims", "")
                )
                if claim in INVOCATION_PRINCIPAL_CLAIMS
            ),
            allowed_principal_attribute_keys=frozenset(
                key
                for key in _comma_separated_values(
                    getattr(settings, "invocation_allowed_principal_attribute_keys", "")
                )
                if is_safe_invocation_principal_attribute_key(key)
            ),
            default_deadline_seconds=float(getattr(settings, "agent_http_timeout_seconds", 30.0)),
        )

    def __post_init__(self) -> None:
        for name in (
            "max_input_bytes",
            "max_context_bytes",
            "max_message_chars",
            "max_output_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_artifact_count", "max_artifact_metadata_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} cannot be negative")
        if (
            isinstance(self.default_deadline_seconds, bool)
            or not isinstance(self.default_deadline_seconds, int | float)
            or not isfinite(self.default_deadline_seconds)
            or self.default_deadline_seconds <= 0
        ):
            raise ValueError("default_deadline_seconds must be positive")

    def effective_input_limit(self, limits: InvocationLimits) -> int:
        return min(
            self.max_input_bytes,
            limits.max_input_bytes if limits.max_input_bytes is not None else self.max_input_bytes,
        )

    def effective_context_limit(self, limits: InvocationLimits) -> int:
        return min(
            self.max_context_bytes,
            limits.max_context_bytes
            if limits.max_context_bytes is not None
            else self.max_context_bytes,
        )

    def effective_message_limit(self, limits: InvocationLimits) -> int:
        return min(
            self.max_message_chars,
            limits.max_message_chars
            if limits.max_message_chars is not None
            else self.max_message_chars,
        )

    def effective_output_limit(self, limits: InvocationLimits) -> int:
        return min(
            self.max_output_bytes,
            limits.max_output_bytes
            if limits.max_output_bytes is not None
            else self.max_output_bytes,
        )

    def effective_artifact_count_limit(self, limits: InvocationLimits) -> int:
        return min(
            self.max_artifact_count,
            limits.max_artifact_count
            if limits.max_artifact_count is not None
            else self.max_artifact_count,
        )

    def effective_artifact_metadata_limit(self, limits: InvocationLimits) -> int:
        return min(
            self.max_artifact_metadata_bytes,
            limits.max_artifact_metadata_bytes
            if limits.max_artifact_metadata_bytes is not None
            else self.max_artifact_metadata_bytes,
        )


InvocationClock = Callable[[], datetime]
_DeadlineValue = TypeVar("_DeadlineValue")
LateInvocationTaskCleanup = Callable[[asyncio.Future[object]], Awaitable[None] | None]
LateInvocationTaskObserver = Callable[
    [asyncio.Future[object], LateInvocationTaskCleanup | None], None
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class InvocationDeadline:
    """One immutable, process-owned deadline shared by an Invocation pipeline.

    The value is established before Connector or Context work starts.  It is
    deliberately distinct from a relative timeout so no nested operation can
    reset the caller's budget by starting a fresh timer.
    """

    deadline_at: datetime
    _clock: InvocationClock = field(repr=False, compare=False)
    _observe_late_task: LateInvocationTaskObserver | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def remaining_seconds(self) -> float:
        return (self.deadline_at - _as_utc(self._clock())).total_seconds()

    def require_remaining(self) -> float:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise InvocationDeadlineExceededError()
        return remaining

    def has_elapsed(self, timestamp: datetime | None) -> bool:
        """Compare a persisted deadline using this invocation's clock."""

        return timestamp is not None and _as_utc(timestamp) <= _as_utc(self._clock())

    async def wait_for(
        self,
        awaitable: Awaitable[_DeadlineValue],
        *,
        on_late_task: LateInvocationTaskCleanup | None = None,
        require_remaining_after_result: bool = True,
    ) -> _DeadlineValue:
        """Await work only through the remaining absolute budget.

        A Connector Resolver or Context Provider may accidentally suppress
        cancellation.  The Runtime still returns at the absolute deadline and
        retains that late task through lifecycle shutdown rather than letting
        it become an unobserved background task. Accepted terminal persistence
        may opt out of the post-result check because its repository-level
        commit guard, rather than a late acknowledgement, establishes the
        durable deadline boundary.
        """

        task = asyncio.ensure_future(awaitable)
        try:
            timeout = self.require_remaining()
        except InvocationDeadlineExceededError:
            # Even an already-expired call may own a shielded Connector
            # release, heartbeat, or durable-write task.  Give the same
            # late-task hook a real Future in this zero-budget branch rather
            # than merely closing a coroutine and losing lifecycle ownership.
            task.cancel()
            self._observe(task, on_late_task)
            raise
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            self._observe(task, on_late_task)
            raise
        if task not in done:
            task.cancel()
            self._observe(task, on_late_task)
            raise InvocationDeadlineExceededError()
        try:
            value = task.result()
        except BaseException:
            # A dependency can fail in the same event-loop turn that consumed
            # the last budget. The absolute deadline wins at the public
            # boundary; while budget remains, preserve the dependency's own
            # failure mapping (for example Connector -> 503 unavailable).
            if self.remaining_seconds() <= 0:
                raise InvocationDeadlineExceededError() from None
            raise
        if require_remaining_after_result:
            try:
                self.require_remaining()
            except InvocationDeadlineExceededError:
                # The dependency completed in the same event-loop turn in
                # which its absolute budget elapsed. It must not be accepted,
                # but a late Connector result still needs its request-scoped
                # release. Register its release with the Runtime rather than
                # awaiting it inline: an uncooperative Connector cleanup
                # cannot extend the mandated pre-acceptance 504 response.
                self._observe(task, on_late_task)
                raise
        return cast(_DeadlineValue, value)

    def _observe(
        self,
        task: asyncio.Future[object],
        on_late_task: LateInvocationTaskCleanup | None,
    ) -> None:
        observer = self._observe_late_task
        if observer is not None:
            observer(task, on_late_task)
            return

        def consume(completed: asyncio.Future[object]) -> None:
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                return

        task.add_done_callback(consume)

    def observe_late_cleanup(self, cleanup: Awaitable[None]) -> None:
        """Retain a post-deadline resource release without delaying the caller."""

        self._observe(asyncio.ensure_future(cleanup), None)


class AgentCallEnvelope(StrictBaseModel):
    """Input made available to one Runtime Adapter execution.

    This is deliberately distinct from ``AgentInvocation``.  The latter stays
    inside Core orchestration and persistence; the Adapter only receives facts
    required to perform a single accepted call.
    """

    model_config = ConfigDict(frozen=True)

    execution_id: str = Field(min_length=1)
    request_id: str | None = None
    session_id: str = Field(min_length=1)
    principal: InvocationPrincipalProjection
    input: JsonDict = Field(default_factory=dict)
    context: InvocationContextProjection = Field(default_factory=InvocationContextProjection)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    deadline_at: datetime
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
    code: StrictStr = Field(min_length=1, max_length=64)
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

    input_tokens: StrictInt | None = Field(default=None, ge=0, le=1_000_000_000)
    output_tokens: StrictInt | None = Field(default=None, ge=0, le=1_000_000_000)
    total_tokens: StrictInt | None = Field(default=None, ge=0, le=1_000_000_000)

    def public_values(self) -> JsonDict:
        return self.model_dump(mode="json", exclude_none=True)


class RawInvocationOutcome(StrictBaseModel):
    """Closed, identity-free Runtime Adapter outcome.

    A successful outcome carries only public result content.  A failed outcome
    carries exactly one ``RawInvocationFailure`` and no Adapter-supplied text
    or output, keeping all public Run/Result projection in the Runtime.
    """

    message: StrictStr = ""
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


class AdapterControlEnvelope(StrictBaseModel):
    """The only identity fact a Runtime Adapter receives for stop control.

    Control never receives an Agent Definition, Connector, request body, or
    Principal projection. The Core has already verified ownership against the
    durable Run; an Adapter can only act on its own process-local execution
    identifier.
    """

    model_config = ConfigDict(frozen=True)

    execution_id: str = Field(min_length=1)


class RawInvocationCancellationOutcome(StrictBaseModel):
    """Closed proof from a control-capable Adapter that execution stopped."""

    stopped: StrictBool


@dataclass(frozen=True, slots=True)
class RuntimeAdapterBinding:
    """Safe, already-resolved Adapter configuration for one invocation."""

    adapter_key: str
    config: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))


RuntimeAdapterExecutor = Callable[
    [RuntimeAdapterBinding, ResolvedConnector | None, AgentCallEnvelope],
    Awaitable[RawInvocationOutcome],
]
RuntimeAdapterCanceller = Callable[
    [RuntimeAdapterBinding, AdapterControlEnvelope],
    Awaitable[RawInvocationCancellationOutcome],
]
RuntimeAdapterConnectorValidator = Callable[[RuntimeAdapterBinding, ResolvedConnector], bool]
RuntimeAdapterConnectorPreparer = Callable[
    [RuntimeAdapterBinding, ResolvedConnector], Awaitable[ResolvedConnector | None]
]

_RAW_OUTCOME_FIELDS = (
    "message",
    "output",
    "artifact_refs",
    "usage",
    "failure",
)
_RAW_USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
_RAW_FAILURE_FIELDS = ("category", "code", "retryable")
_ARTIFACT_REFERENCE_FIELDS = ("artifact_id", "type", "uri", "title", "metadata")
_ENVELOPE_RESERVED_INPUT_KEYS = frozenset(
    {
        "artifact_refs",
        "authorization",
        "context",
        "definition",
        "definition_snapshot",
        "frontend_context",
        "headers",
        "host_metadata",
        "host_context",
        "internal_context",
        "invocation",
        "knowledge_context",
        "memory_context",
        "metadata",
        "principal",
        "route",
        "route_metadata",
        "router",
        "router_metadata",
        "token",
        "trace",
        "trace_metadata",
        "user",
        "user_context",
    }
)
_SENSITIVE_INPUT_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "header",
    "password",
    "principal_envelope",
    "secret",
    "signature",
    "token",
)
_INPUT_KEY_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")
_SAFE_ARTIFACT_METADATA_KEYS = frozenset({"content_type", "sha256", "size_bytes"})
_SAFE_CONTENT_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.+/-]{0,127}$")
_SAFE_SHA256_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class RuntimeAdapterExecution:
    """One Adapter callable captured during Binding Resolution.

    The callable is captured exactly once so the execution path does not look
    up a mutable Adapter method after preflight has succeeded.
    """

    binding: RuntimeAdapterBinding
    execute: RuntimeAdapterExecutor
    connector: ResolvedConnector | None = None
    connector_validator: RuntimeAdapterConnectorValidator | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    connector_preparer: RuntimeAdapterConnectorPreparer | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    requires_connector: bool = False
    limits: InvocationLimits = field(default_factory=InvocationLimits)
    requires_knowledge_context: bool = False
    principal_claims: frozenset[InvocationPrincipalClaim] = frozenset()
    principal_attribute_keys: frozenset[str] = frozenset()
    late_task_cleanup: Callable[[], Awaitable[None]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # Append new protocol surface after the established execution fields so
    # deployment code using positional construction keeps its old meaning.
    cancel: RuntimeAdapterCanceller | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    async def invoke(self, envelope: AgentCallEnvelope) -> RawInvocationOutcome:
        candidate = self.execute(self.binding, self.connector, envelope)
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
                {
                    field: _untrusted_outcome_field_value(field, values[field])
                    for field in _RAW_OUTCOME_FIELDS
                    if field in values
                }
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise _InvalidRuntimeAdapterOutcome from None


_RuntimeExecutionState = Literal[
    "pre_dispatch",
    "dispatched",
    "stop_confirmed",
    "stop_unconfirmed",
    "terminalizing",
]
RuntimeControlState = Literal[
    "stop_confirmed",
    "stop_unconfirmed",
    "unsupported",
    "terminal",
]


@dataclass(frozen=True, slots=True)
class RuntimeExecutionIdentity:
    """The durable ownership facts that authorize one Runtime control action."""

    run_id: str
    tenant_id: str | None
    user_id: str | None
    session_id: str
    agent_id: str


@dataclass(frozen=True, slots=True)
class RuntimeCancellationDecision:
    """One safe control observation; it never exposes Adapter diagnostics."""

    control_state: RuntimeControlState
    completion_certainty: Literal["certain", "unknown"] | None = None
    reason_code: str | None = None


@dataclass(slots=True)
class _RuntimeExecutionControl:
    identity: RuntimeExecutionIdentity
    binding: RuntimeAdapterBinding
    canceller: RuntimeAdapterCanceller | None
    state: _RuntimeExecutionState = "pre_dispatch"
    dispatched: bool = False
    cancellation_task: asyncio.Future[object] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _control_decision(control: _RuntimeExecutionControl) -> RuntimeCancellationDecision:
    """Project one locked control record without exposing Adapter details."""

    if control.state == "stop_confirmed":
        return RuntimeCancellationDecision(
            control_state="stop_confirmed",
            completion_certainty=("unknown" if control.dispatched else "certain"),
        )
    if control.state == "stop_unconfirmed":
        return RuntimeCancellationDecision(
            control_state="stop_unconfirmed",
            completion_certainty="unknown",
            reason_code="control_stop_unconfirmed",
        )
    if control.state == "terminalizing":
        return RuntimeCancellationDecision(
            control_state="terminal",
            completion_certainty=("unknown" if control.dispatched else "certain"),
            reason_code="control_terminalizing",
        )
    # The task that performs control always settles ``dispatched`` to a closed
    # fact. Retain a conservative fallback in case a future Adapter integration
    # violates that internal promise.
    return RuntimeCancellationDecision(
        control_state="stop_unconfirmed",
        completion_certainty="unknown",
        reason_code="control_stop_unconfirmed",
    )


class InvocationRuntime:
    """Process-owned Runtime that projects Adapter outcomes into public results."""

    def __init__(
        self,
        *,
        policy: InvocationRuntimePolicy | None = None,
        clock: InvocationClock | None = None,
    ) -> None:
        self._policy = policy or InvocationRuntimePolicy()
        self._clock = clock or _utc_now
        # A cancellation-defiant Adapter task must remain strongly owned until
        # it completes.  Otherwise it can be garbage-collected before
        # application shutdown drains it, leaving it to use a disposed Adapter
        # or request-scoped Connector.  This is not a Connector cache: each
        # task is removed, together with its cleanup hook, as soon as it ends.
        self._late_adapter_tasks: set[asyncio.Future[object]] = set()
        self._late_adapter_cleanup_tasks: set[asyncio.Future[object]] = set()
        self._late_preflight_tasks: set[asyncio.Future[object]] = set()
        self._late_preflight_cleanup_tasks: set[asyncio.Future[object]] = set()
        # A disconnected HTTP client must not cancel a Run that was already
        # accepted.  These tasks retain the complete Connector scope and
        # persistence convergence until they finish, or application shutdown
        # explicitly takes control of them.
        self._accepted_execution_tasks: set[asyncio.Future[object]] = set()
        # A cancelled durable Run start may have committed before its
        # acknowledgement was delivered. These reconciliation tasks never
        # redispatch an Adapter; they only read the stable Run identity and
        # converge it or safely release its fenced Plan claim.
        self._durable_start_reconciliation_tasks: set[asyncio.Future[object]] = set()
        # One record exists only for an accepted in-process Invocation. It
        # contains safe identity/binding facts and no Connector, Definition,
        # input, or Principal payload. A process restart deliberately loses the
        # record; an otherwise-running durable Run then reports unconfirmed
        # control instead of pretending it can stop an unknown remote effect.
        self._execution_controls: dict[str, _RuntimeExecutionControl] = {}
        self._control_tasks: set[asyncio.Future[object]] = set()

    def register_execution_control(
        self,
        *,
        invocation: AgentInvocation,
        agent_id: str,
        execution: RuntimeAdapterExecution | None,
    ) -> None:
        """Register pre-dispatch control facts immediately after Run acceptance.

        The surrounding service invokes this synchronously after its durable
        start await returns and before the next await. That makes the record
        visible with the accepted Run while ensuring a losing concurrent start
        attempt never leaves a process-local control record for another
        worker's Run.
        """

        self._execution_controls[invocation.run_id] = _RuntimeExecutionControl(
            identity=RuntimeExecutionIdentity(
                run_id=invocation.run_id,
                tenant_id=invocation.user.tenant_id,
                user_id=invocation.user.id,
                session_id=invocation.session_id,
                agent_id=agent_id,
            ),
            binding=(
                execution.binding
                if execution is not None
                else RuntimeAdapterBinding(adapter_key="legacy_v2", config={})
            ),
            canceller=execution.cancel if execution is not None else None,
        )

    async def mark_dispatch_started(self, run_id: str) -> bool:
        """Record the exact dispatch edge and honor a prior stop proof.

        ``False`` means a pre-dispatch cancellation was already confirmed, so
        the caller must converge the accepted Run as cancelled without calling
        the Adapter.
        """

        control = self._execution_controls.get(run_id)
        if control is None:
            return True
        async with control.lock:
            if control.state in {"stop_confirmed", "terminalizing"}:
                return False
            control.state = "dispatched"
            control.dispatched = True
            return True

    async def begin_terminalization(self, run_id: str) -> bool:
        """Close control before a durable Run/Result transition starts.

        A late cancellation must not call an Adapter after the Runtime already
        chose a result. The boolean preserves an earlier confirmed stop so the
        caller can project the canonical ``cancelled`` terminal result.
        """

        control = self._execution_controls.get(run_id)
        if control is None:
            return False
        async with control.lock:
            confirmed = control.state == "stop_confirmed"
            control.state = "terminalizing"
            return confirmed

    def complete_execution_control(self, run_id: str) -> None:
        """Release ephemeral control state after the canonical terminal write."""

        self._execution_controls.pop(run_id, None)

    async def request_cancellation(
        self,
        identity: RuntimeExecutionIdentity,
    ) -> RuntimeCancellationDecision:
        """Apply capability-gated stop control to one already-owned execution.

        Missing process state is deliberately not interpreted as a completed
        stop: the durable Run may have survived a worker restart or an unknown
        remote dispatch. Only the Runtime's pre-dispatch fact or a closed
        Adapter confirmation can move the state to ``stop_confirmed``.
        """

        control = self._execution_controls.get(identity.run_id)
        if control is None:
            return RuntimeCancellationDecision(
                control_state="stop_unconfirmed",
                completion_certainty="unknown",
                reason_code="control_state_unavailable",
            )
        if control.identity != identity:
            return RuntimeCancellationDecision(
                control_state="unsupported",
                completion_certainty="unknown",
                reason_code="control_identity_unavailable",
            )

        task: asyncio.Future[object] | None = None
        async with control.lock:
            if control.state == "pre_dispatch":
                control.state = "stop_confirmed"
                return RuntimeCancellationDecision(
                    control_state="stop_confirmed",
                    completion_certainty="certain",
                )
            if control.state == "stop_confirmed":
                return RuntimeCancellationDecision(
                    control_state="stop_confirmed",
                    completion_certainty=("unknown" if control.dispatched else "certain"),
                )
            if control.state == "stop_unconfirmed":
                return RuntimeCancellationDecision(
                    control_state="stop_unconfirmed",
                    completion_certainty="unknown",
                    reason_code="control_stop_unconfirmed",
                )
            if control.state == "terminalizing":
                return RuntimeCancellationDecision(
                    control_state="terminal",
                    completion_certainty=("unknown" if control.dispatched else "certain"),
                    reason_code="control_terminalizing",
                )
            if control.canceller is None:
                return RuntimeCancellationDecision(
                    control_state="unsupported",
                    completion_certainty="unknown",
                    reason_code="control_unsupported",
                )
            task = control.cancellation_task
            if task is None:
                task = asyncio.ensure_future(self._call_adapter_canceller(control))
                control.cancellation_task = task
                self._retain_control_task(task)

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A disconnected control caller must not cancel the process-owned
            # Adapter control task. Its eventual safe outcome remains in the
            # Runtime state and is observed during lifecycle shutdown.
            raise
        except Exception:  # pragma: no cover - the task closes all failures.
            pass

        async with control.lock:
            return _control_decision(control)

    async def _call_adapter_canceller(self, control: _RuntimeExecutionControl) -> None:
        """Run and atomically record one closed Adapter control observation.

        The control task owns the state transition instead of the awaiting HTTP
        request.  This closes the race where a cooperative Adapter cancels the
        execute Task, the invocation pipeline terminalizes it, and only then
        the control caller gets a chance to record its already-confirmed stop.
        """

        canceller = control.canceller
        stopped = False
        try:
            if canceller is None:  # pragma: no cover - caller gates this branch.
                return
            candidate = canceller(
                control.binding,
                AdapterControlEnvelope(execution_id=control.identity.run_id),
            )
            if not isawaitable(candidate):
                return
            else:
                outcome = await candidate
                if isinstance(outcome, RawInvocationCancellationOutcome):
                    values = object.__getattribute__(outcome, "__dict__")
                    validated = RawInvocationCancellationOutcome.model_validate(
                        {"stopped": values.get("stopped")}
                    )
                    stopped = validated.stopped is True
        except (asyncio.CancelledError, Exception):
            stopped = False
        finally:
            async with control.lock:
                # Terminalization won the race if it began before this control
                # proof returned. A late Adapter answer must never reopen or
                # overwrite the canonical result selected by the Runtime.
                if control.state == "dispatched":
                    control.state = "stop_confirmed" if stopped else "stop_unconfirmed"

    def _retain_control_task(self, task: asyncio.Future[object]) -> None:
        """Keep an in-flight stop attempt process-owned through shutdown."""

        self._control_tasks.add(task)

        def consume(completed: asyncio.Future[object]) -> None:
            self._control_tasks.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                return

        task.add_done_callback(consume)

    def establish_deadline(self, invocation: AgentInvocation) -> AgentInvocation:
        """Stamp the sole absolute deadline at pipeline entry.

        A caller may only tighten this process policy.  The returned immutable
        Invocation is then carried through Connector resolution, Context
        preflight, the Envelope and durable Run record.
        """

        deadline_at = _resolve_deadline(
            invocation.deadline_at,
            default_deadline_seconds=self._policy.default_deadline_seconds,
            now=self._clock(),
        )
        return invocation.model_copy(update={"deadline_at": deadline_at})

    def deadline_for(self, invocation: AgentInvocation) -> InvocationDeadline:
        """Return the already-established deadline for an Invocation."""

        established = self.establish_deadline(invocation)
        deadline_at = established.deadline_at
        if deadline_at is None:  # pragma: no cover - establish_deadline guarantees it.
            raise InvocationDeadlineExceededError()
        return InvocationDeadline(
            deadline_at=deadline_at,
            _clock=self._clock,
            _observe_late_task=self._observe_late_preflight_task,
        )

    def _preflight_established(
        self,
        *,
        execution: RuntimeAdapterExecution,
        invocation: AgentInvocation,
        input_schema: Mapping[str, object] | None = None,
    ) -> AgentCallEnvelope:
        """Validate an Invocation whose absolute deadline is already fixed."""

        deadline = self.deadline_for(invocation)
        deadline.require_remaining()
        # Callers that assemble Context first use ``preflight_input`` before
        # any Provider sees request data. Repeating it here keeps direct
        # Runtime callers safe and makes this method self-contained.
        self.preflight_input(
            execution=execution,
            invocation=invocation,
            input_schema=input_schema,
        )
        envelope = _build_call_envelope(
            invocation,
            input_schema=input_schema,
            deadline_at=deadline.deadline_at,
            principal_claims=(execution.principal_claims & self._policy.allowed_principal_claims),
            principal_attribute_keys=(
                execution.principal_attribute_keys & self._policy.allowed_principal_attribute_keys
            ),
        )
        if _context_size(invocation) > self._policy.effective_context_limit(execution.limits):
            raise InvocationPreflightRejectedError("invocation_context_limit_exceeded")
        if execution.requires_knowledge_context and not envelope.context.knowledge:
            raise InvocationPreflightRejectedError("invocation_required_context_missing")
        if not _artifact_refs_are_safe(
            envelope.artifact_refs,
            max_count=self._policy.effective_artifact_count_limit(execution.limits),
            max_metadata_bytes=self._policy.effective_artifact_metadata_limit(execution.limits),
        ):
            raise InvocationPreflightRejectedError("invocation_artifact_invalid")
        deadline.require_remaining()
        return envelope

    async def start(self) -> None:
        """Participate in the application lifecycle before Adapter use begins."""

    async def stop(self) -> None:
        """Cancel and drain Adapter tasks that outlived their call deadline.

        Normal calls are owned by their request.  Only cancellation-defiant
        tasks are retained here, so application shutdown can stop observing
        them before the Catalog releases the underlying Adapters.  The owning
        Application Runtime still applies its bounded cleanup deadline to this
        operation.
        """

        while (
            self._accepted_execution_tasks
            or self._durable_start_reconciliation_tasks
            or self._late_adapter_tasks
            or self._late_adapter_cleanup_tasks
            or self._late_preflight_tasks
            or self._late_preflight_cleanup_tasks
            or self._control_tasks
        ):
            accepted_tasks = tuple(self._accepted_execution_tasks)
            for task in accepted_tasks:
                task.cancel()
            if accepted_tasks:
                await _drain_shutdown_tasks(accepted_tasks)
                self._accepted_execution_tasks.difference_update(accepted_tasks)

            tasks = tuple(self._late_adapter_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await _drain_shutdown_tasks(tasks)
                # A completed Task's done callbacks may be queued behind the
                # gather continuation. Remove this known-complete batch here
                # as well, so shutdown cannot busy-loop before those callbacks
                # get a chance to discard it.
                self._late_adapter_tasks.difference_update(tasks)

            cleanup_tasks = tuple(self._late_adapter_cleanup_tasks)
            if cleanup_tasks:
                for task in cleanup_tasks:
                    task.cancel()
                await _drain_shutdown_tasks(cleanup_tasks)
                self._late_adapter_cleanup_tasks.difference_update(cleanup_tasks)

            preflight_tasks = tuple(self._late_preflight_tasks)
            for task in preflight_tasks:
                task.cancel()
            if preflight_tasks:
                await _drain_shutdown_tasks(preflight_tasks)
                self._late_preflight_tasks.difference_update(preflight_tasks)

            preflight_cleanup_tasks = tuple(self._late_preflight_cleanup_tasks)
            if preflight_cleanup_tasks:
                for task in preflight_cleanup_tasks:
                    task.cancel()
                await _drain_shutdown_tasks(preflight_cleanup_tasks)
                self._late_preflight_cleanup_tasks.difference_update(preflight_cleanup_tasks)

            control_tasks = tuple(self._control_tasks)
            if control_tasks:
                for task in control_tasks:
                    task.cancel()
                await _drain_shutdown_tasks(control_tasks)
                self._control_tasks.difference_update(control_tasks)

            reconciliation_tasks = tuple(self._durable_start_reconciliation_tasks)
            if reconciliation_tasks:
                # Drain every Adapter/Connector task first. A durable fence
                # makes an unreadable start record restart-safe, but a short
                # transient read can still converge it during normal shutdown.
                # If the enclosing Application cleanup bound fires, only this
                # no-dispatch reconciliation remains; it cannot keep a live
                # Adapter running after Catalog disposal.
                await _drain_shutdown_tasks(reconciliation_tasks)
                self._durable_start_reconciliation_tasks.difference_update(reconciliation_tasks)

    def preflight(
        self,
        *,
        execution: RuntimeAdapterExecution,
        invocation: AgentInvocation,
        input_schema: Mapping[str, object] | None = None,
    ) -> AgentCallEnvelope:
        """Construct and bound the Adapter envelope before accepting a Run."""

        return self._preflight_established(
            execution=execution,
            invocation=self.establish_deadline(invocation),
            input_schema=input_schema,
        )

    def preflight_input(
        self,
        *,
        execution: RuntimeAdapterExecution,
        invocation: AgentInvocation,
        input_schema: Mapping[str, object] | None = None,
        reject_reserved_request_keys: bool = False,
    ) -> JsonDict:
        """Return the only request input that may reach Context Providers.

        Context assembly can consult Memory and Knowledge Providers, so it may
        not receive raw HTTP-shaped request input. This no-side-effect phase is
        deliberately usable before Context assembly and before a Plan claim.
        """

        if reject_reserved_request_keys and _contains_reserved_input_key(invocation.input):
            raise InvocationPreflightRejectedError("invocation_input_invalid")
        projected_input = _project_declared_input(invocation.input, input_schema=input_schema)
        _validate_projected_input(projected_input, input_schema=input_schema)
        if _json_size(projected_input) > self._policy.effective_input_limit(execution.limits):
            raise InvocationPreflightRejectedError("invocation_input_limit_exceeded")
        # The first pipeline step stamps a concrete absolute deadline. Direct
        # Runtime callers may invoke this method independently, so establish
        # and validate one here too; an existing deadline is never extended.
        self.deadline_for(invocation).require_remaining()
        if not _artifact_refs_are_safe(
            invocation.artifact_refs,
            max_count=self._policy.effective_artifact_count_limit(execution.limits),
            max_metadata_bytes=self._policy.effective_artifact_metadata_limit(execution.limits),
        ):
            raise InvocationPreflightRejectedError("invocation_artifact_invalid")
        return projected_input

    def attach_trusted_plan_idempotency(
        self,
        envelope: AgentCallEnvelope,
        invocation: AgentInvocation,
    ) -> AgentCallEnvelope:
        """Add the canonical Plan key after Core has atomically claimed its Step.

        Preflight deliberately happens before a Plan claim so a rejected call
        does not mutate Plan state.  The claim is the only trusted source for
        this optional Adapter cache key, so Core attaches it afterwards without
        rebuilding the already approved envelope or extending its deadline.
        """

        return envelope.model_copy(
            update={"idempotency_key": _canonical_plan_idempotency_key(invocation)}
        )

    async def execute(
        self,
        *,
        execution: RuntimeAdapterExecution,
        invocation: AgentInvocation,
        agent_id: str,
        input_schema: Mapping[str, object] | None = None,
        output_schema: Mapping[str, object] | None = None,
    ) -> AgentInvocationResult:
        invocation = self.establish_deadline(invocation)
        envelope = self._preflight_established(
            execution=execution,
            invocation=invocation,
            input_schema=input_schema,
        )
        return await self.execute_preflighted(
            execution=execution,
            envelope=envelope,
            invocation=invocation,
            agent_id=agent_id,
            output_schema=output_schema,
        )

    async def execute_legacy_v2(
        self,
        *,
        invoke: Callable[[], Awaitable[AgentInvocationResult]],
        invocation: AgentInvocation,
        agent_id: str,
    ) -> AgentInvocationResult:
        """Bound a retained ``invoke_v2`` Adapter by the shared deadline.

        ``legacy_v2`` remains a transition protocol, but it is still an
        accepted Invocation path. It therefore cannot reset or bypass the
        process-owned deadline merely because it does not consume an
        ``AgentCallEnvelope`` yet.
        """

        deadline = self.deadline_for(invocation)
        remaining = deadline.remaining_seconds()
        if remaining <= 0:
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "deadline_exceeded",
                    retryable=False,
                ),
                completion_certainty="certain",
            )
        try:
            result = await _await_runtime_value(
                invoke(),
                timeout=remaining,
                observe_late_task=lambda task: self._observe_late_adapter_task(
                    task,
                    late_task_cleanup=None,
                ),
            )
        except asyncio.CancelledError as exc:
            return self._project_exception_or_expired_deadline(
                deadline=deadline,
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
                completion_certainty="unknown",
            )
        except TimeoutError as exc:
            return self._project_exception_or_expired_deadline(
                deadline=deadline,
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
                completion_certainty="unknown",
            )
        except Exception as exc:
            return self._project_exception_or_expired_deadline(
                deadline=deadline,
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
                completion_certainty="unknown",
            )
        if deadline.remaining_seconds() <= 0:
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "deadline_exceeded",
                    retryable=False,
                ),
                completion_certainty="unknown",
            )
        if not isinstance(result, AgentInvocationResult):
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "invalid_response",
                    retryable=False,
                ),
            )
        if result.status in {"completed", "blocked", "clarify"}:
            # ``blocked``/``clarify`` are controlled non-terminal Adapter
            # outcomes rather than evidence of a transport failure. They
            # remain valid across the temporary legacy_v2 bridge after the
            # shared deadline check above.
            return result
        # A retained v2 Adapter has already crossed the dispatch boundary.
        # Its public result is an old compatibility shape, so it cannot
        # attest that a remote side effect did not happen. Re-project it
        # through the closed Runtime failure vocabulary and forbid redispatch.
        category = _legacy_failure_category(result)
        return self.project_failure(
            invocation=invocation,
            agent_id=agent_id,
            failure=RawInvocationFailure.for_category(category, retryable=False),
            completion_certainty=(
                "unknown" if category in {"remote_failure", "deadline_exceeded"} else None
            ),
        )

    async def execute_preflighted(
        self,
        *,
        execution: RuntimeAdapterExecution,
        envelope: AgentCallEnvelope,
        invocation: AgentInvocation,
        agent_id: str,
        output_schema: Mapping[str, object] | None = None,
    ) -> AgentInvocationResult:
        """Execute an Envelope that was constructed before Invocation acceptance.

        InvocationService uses this after its pre-acceptance check so the
        Adapter receives exactly the deadline and allowlisted projection that
        were validated before a Run existed. Direct Runtime callers may use
        :meth:`execute`, which performs that same preflight itself.
        """

        # Keep the deadline outside Adapter-owned state. The Envelope is frozen
        # and the Adapter gets a deep copy, but this local value also protects
        # the terminal decision from a hostile ``object.__setattr__`` bypass.
        deadline_at = envelope.deadline_at
        deadline = InvocationDeadline(deadline_at=deadline_at, _clock=self._clock)
        remaining = deadline.remaining_seconds()
        if remaining <= 0:
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "deadline_exceeded",
                    retryable=False,
                ),
                completion_certainty="certain",
            )
        try:
            outcome = await _await_runtime_value(
                execution.invoke(envelope.model_copy(deep=True)),
                timeout=remaining,
                observe_late_task=lambda task: self._observe_late_adapter_task(
                    task,
                    late_task_cleanup=execution.late_task_cleanup,
                ),
            )
        except asyncio.CancelledError as exc:
            # An accepted Adapter may already have dispatched remote work. A
            # cancellation is never evidence that its side effect stopped.
            return self._project_exception_or_expired_deadline(
                deadline=deadline,
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
                completion_certainty="unknown",
            )
        except TimeoutError as exc:
            return self._project_exception_or_expired_deadline(
                deadline=deadline,
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
                completion_certainty="unknown",
            )
        except _InvalidRuntimeAdapterOutcome as exc:
            return self._project_exception_or_expired_deadline(
                deadline=deadline,
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
            )
        except Exception as exc:
            return self._project_exception_or_expired_deadline(
                deadline=deadline,
                invocation=invocation,
                agent_id=agent_id,
                exc=exc,
                completion_certainty="unknown",
            )
        if deadline.remaining_seconds() <= 0:
            # A task that completed in the same event-loop turn as the timer
            # cannot be permitted to turn an expired absolute deadline into a
            # success merely because scheduling observed its result first.
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "deadline_exceeded",
                    retryable=False,
                ),
                completion_certainty="unknown",
            )
        if outcome.failure is not None:
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=outcome.failure,
                # A remote/deadline failure can occur after the request left
                # Core. Closed local unavailable/rejected categories retain
                # their documented retry semantics instead.
                completion_certainty=(
                    "unknown"
                    if outcome.failure.category in {"remote_failure", "deadline_exceeded"}
                    else None
                ),
            )
        output_size = _json_size_or_none(outcome.output)
        if (
            len(outcome.message) > self._policy.effective_message_limit(execution.limits)
            or output_size < 0
            or output_size > self._policy.effective_output_limit(execution.limits)
            or not _artifact_refs_are_safe(
                outcome.artifact_refs,
                max_count=self._policy.effective_artifact_count_limit(execution.limits),
                max_metadata_bytes=self._policy.effective_artifact_metadata_limit(execution.limits),
            )
        ):
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "invalid_response",
                    retryable=False,
                ),
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

    def _project_exception_or_expired_deadline(
        self,
        *,
        deadline: InvocationDeadline,
        invocation: AgentInvocation,
        agent_id: str,
        exc: BaseException,
        completion_certainty: Literal["certain", "unknown"] | None = None,
    ) -> AgentInvocationResult:
        """Let an elapsed absolute deadline win over a same-turn exception."""

        if deadline.remaining_seconds() <= 0:
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "deadline_exceeded",
                    retryable=False,
                ),
                completion_certainty="unknown",
            )
        return self.project_exception(
            invocation=invocation,
            agent_id=agent_id,
            exc=exc,
            completion_certainty=completion_certainty,
        )

    def project_exception(
        self,
        *,
        invocation: AgentInvocation,
        agent_id: str,
        exc: BaseException,
        completion_certainty: Literal["certain", "unknown"] | None = None,
    ) -> AgentInvocationResult:
        """Map an Adapter exception without trusting its message or details."""

        if isinstance(exc, TimeoutError | InvocationDeadlineExceededError):
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
            completion_certainty=completion_certainty,
        )

    def project_failure(
        self,
        *,
        invocation: AgentInvocation,
        agent_id: str,
        failure: RawInvocationFailure,
        completion_certainty: Literal["certain", "unknown"] | None = None,
    ) -> AgentInvocationResult:
        """Create the only public projection for an accepted Adapter failure."""

        # Timeout/cancel uncertainty is never an automatic redispatch signal.
        # Adapter code may report a safe category, but it cannot turn a
        # possibly side-effecting deadline outcome into a retryable one.
        retryable = (
            False
            if failure.category == "deadline_exceeded" or completion_certainty == "unknown"
            else failure.retryable
        )
        details: JsonDict = {
            "category": failure.category,
            "retryable": retryable,
        }
        if completion_certainty is not None:
            details["completion_certainty"] = completion_certainty
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=agent_id,
            status="failed",
            message=_PUBLIC_FAILURE_MESSAGE,
            error=ErrorDetail(
                code=failure.code,
                message=_PUBLIC_FAILURE_MESSAGE,
                details=details,
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
        if output_schema is None:
            return result
        required = output_schema.get("required")
        if result.output is None and not required:
            # Message-only success remains a valid v2 outcome unless the
            # Definition explicitly requires a structured output field.
            return result
        try:
            validate_json_schema(instance=result.output, schema=dict(output_schema))
        except (JsonSchemaValidationError, JsonSchemaSchemaError):
            return self.project_failure(
                invocation=invocation,
                agent_id=agent_id,
                failure=RawInvocationFailure.for_category(
                    "invalid_response",
                    retryable=False,
                ),
            )
        return result

    def retain_accepted_execution(self, task: asyncio.Future[object]) -> None:
        """Keep an accepted server-side execution alive after client disconnect.

        The task owns the whole Connector scope and Run/Result completion
        sequence.  It is intentionally retained only after acceptance; a
        disconnected client cannot turn that accepted work into a remote
        cancellation request.
        """

        self._accepted_execution_tasks.add(task)

        def consume(completed: asyncio.Future[object]) -> None:
            self._accepted_execution_tasks.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                # The task itself owns safe Run convergence. Never surface a
                # private Adapter or Connector exception from a detached call.
                return

        task.add_done_callback(consume)

    def retain_durable_start_reconciliation(self, task: asyncio.Future[object]) -> None:
        """Strongly retain a no-dispatch start-outcome reconciliation task."""

        self._durable_start_reconciliation_tasks.add(task)

        def consume(completed: asyncio.Future[object]) -> None:
            self._durable_start_reconciliation_tasks.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                return

        task.add_done_callback(consume)

    def _observe_late_preflight_task(
        self,
        task: asyncio.Future[object],
        late_task_cleanup: LateInvocationTaskCleanup | None,
    ) -> None:
        """Retain a cancelled Context/Connector task until it really stops."""

        self._late_preflight_tasks.add(task)
        cleanup = late_task_cleanup(task) if late_task_cleanup is not None else None

        def consume(completed: asyncio.Future[object]) -> None:
            self._late_preflight_tasks.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                pass
            finally:
                if cleanup is not None:
                    self._observe_late_preflight_cleanup(cleanup)

        task.add_done_callback(consume)

    def _observe_late_preflight_cleanup(self, cleanup: Awaitable[None]) -> None:
        """Retain deferred Connector release after a late preflight task."""

        task = asyncio.ensure_future(cleanup)
        self._late_preflight_cleanup_tasks.add(task)

        def consume(completed: asyncio.Future[object]) -> None:
            self._late_preflight_cleanup_tasks.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                return

        task.add_done_callback(consume)

    def _observe_late_adapter_task(
        self,
        task: asyncio.Future[object],
        *,
        late_task_cleanup: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Retain and drain one deadline-expired Adapter task and its lease."""

        self._late_adapter_tasks.add(task)
        cleanup = late_task_cleanup() if late_task_cleanup is not None else None

        def consume(completed: asyncio.Future[object]) -> None:
            self._late_adapter_tasks.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                pass
            finally:
                if cleanup is not None:
                    self._observe_late_adapter_cleanup(cleanup)

        task.add_done_callback(consume)

    def _observe_late_adapter_cleanup(self, cleanup: Awaitable[None]) -> None:
        """Keep deferred request-resource release in the lifecycle drain set."""

        task = asyncio.ensure_future(cleanup)
        self._late_adapter_cleanup_tasks.add(task)

        def consume(completed: asyncio.Future[object]) -> None:
            self._late_adapter_cleanup_tasks.discard(completed)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                return

        task.add_done_callback(consume)


class _InvalidRuntimeAdapterOutcome(Exception):
    """Internal marker; its details never cross the Runtime boundary."""


async def _drain_shutdown_tasks(tasks: tuple[asyncio.Future[object], ...]) -> None:
    """Drain a cancellation batch without making caller cancellation unbounded.

    A Connector/Adapter supplied by a deployment is allowed to suppress
    ``CancelledError``. When the Application Runtime's cleanup deadline
    cancels :meth:`InvocationRuntime.stop`, shielding the gather lets that
    caller return immediately while the Runtime's strong task sets continue
    to own the late work. The Application Runtime then records the bounded
    cleanup timeout instead of waiting forever and blocking Catalog disposal.
    """

    drain = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.shield(drain)
    except asyncio.CancelledError:
        # Keep the gather alive long enough to consume its task outcomes. The
        # individual Futures remain strongly retained by InvocationRuntime
        # until their own callbacks discard them.
        drain.add_done_callback(_consume_shutdown_drain)
        raise


def _consume_shutdown_drain(completed: asyncio.Future[object]) -> None:
    """Retrieve detached shutdown outcomes without rendering private errors."""

    try:
        completed.result()
    except (asyncio.CancelledError, Exception):
        return


def _untrusted_outcome_field_value(field: str, value: object) -> object:
    """Turn known bypassable Pydantic child instances back into raw values.

    An Adapter can return ``model_construct`` objects. Passing a nested model
    instance directly to Pydantic can skip child revalidation, so only the
    closed field sets below are read without serializing or warning. Unknown
    fields are dropped with the parent outcome's allowlist.
    """

    if field == "usage" and isinstance(value, RawInvocationUsage):
        return _declared_model_values(value, _RAW_USAGE_FIELDS)
    if field == "failure" and isinstance(value, RawInvocationFailure):
        return _declared_model_values(value, _RAW_FAILURE_FIELDS)
    if field == "artifact_refs" and isinstance(value, list | tuple):
        return [
            _declared_model_values(item, _ARTIFACT_REFERENCE_FIELDS)
            if isinstance(item, ArtifactRef)
            else item
            for item in value
        ]
    return value


def _legacy_failure_category(result: AgentInvocationResult) -> InvocationFailureCategory:
    """Map retained v2 result codes back into the closed Runtime vocabulary."""

    code = result.error.code if result.error is not None else None
    categories: dict[str, InvocationFailureCategory] = {
        "invocation_unavailable": "unavailable",
        "invocation_deadline_exceeded": "deadline_exceeded",
        "invocation_rejected": "rejected",
        "invocation_remote_failure": "remote_failure",
        "invocation_invalid_response": "invalid_response",
    }
    return categories.get(code or "", "remote_failure")


def _declared_model_values(model: object, fields: tuple[str, ...]) -> dict[str, object]:
    values = object.__getattribute__(model, "__dict__")
    return {field: values[field] for field in fields if field in values}


def _build_call_envelope(
    invocation: AgentInvocation,
    *,
    input_schema: Mapping[str, object] | None = None,
    deadline_at: datetime,
    principal_claims: frozenset[InvocationPrincipalClaim] = frozenset(),
    principal_attribute_keys: frozenset[str] = frozenset(),
) -> AgentCallEnvelope:
    idempotency_key = _canonical_plan_idempotency_key(invocation)
    return AgentCallEnvelope(
        execution_id=invocation.run_id,
        request_id=invocation.request_id,
        session_id=invocation.session_id,
        principal=_project_principal(
            invocation,
            claims=principal_claims,
            attribute_keys=principal_attribute_keys,
        ),
        input=_project_declared_input(invocation.input, input_schema=input_schema),
        context=_project_governed_context(invocation),
        artifact_refs=[reference.model_copy(deep=True) for reference in invocation.artifact_refs],
        deadline_at=deadline_at,
        idempotency_key=idempotency_key,
    )


def _resolve_deadline(
    deadline_at: datetime | None,
    *,
    default_deadline_seconds: float,
    now: datetime,
) -> datetime:
    now = _as_utc(now)
    if deadline_at is None:
        return now + timedelta(seconds=default_deadline_seconds)
    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise InvocationPreflightRejectedError("invocation_deadline_invalid")
    resolved = deadline_at.astimezone(UTC)
    if resolved <= now:
        raise InvocationDeadlineExceededError()
    return min(resolved, now + timedelta(seconds=default_deadline_seconds))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Invocation clock must return an aware datetime")
    return value.astimezone(UTC)


def _close_awaitable(awaitable: object) -> None:
    """Avoid an unawaited coroutine warning on an already-expired deadline."""

    cancel = getattr(awaitable, "cancel", None)
    if callable(cancel):
        cancel()
        return
    closer = getattr(awaitable, "close", None)
    if callable(closer):
        closer()


def _project_declared_input(
    value: JsonDict,
    *,
    input_schema: Mapping[str, object] | None,
) -> JsonDict:
    """Copy only Definition-declared business input into an Adapter envelope."""

    properties = input_schema.get("properties") if input_schema is not None else None
    declared = set(properties) if isinstance(properties, Mapping) else None
    projected: JsonDict = {}
    for key, item in value.items():
        if not isinstance(key, str) or _is_reserved_input_key(key):
            # Service entry points have already sanitized raw request input
            # before Context assembly.  The second full preflight also sees
            # Core's own memory/knowledge candidates here; omit both sources
            # from the Envelope rather than treating internal Context as a
            # client error.
            continue
        if declared is not None and key not in declared:
            continue
        if _contains_reserved_input_key(item):
            raise InvocationPreflightRejectedError("invocation_input_invalid")
        projected[key] = deepcopy(item)
    return projected


def _is_reserved_input_key(value: str) -> bool:
    normalized = _INPUT_KEY_SEPARATOR_PATTERN.sub("_", value.casefold()).strip("_")
    collapsed = normalized.replace("_", "")
    return normalized in _ENVELOPE_RESERVED_INPUT_KEYS or any(
        marker in normalized or marker.replace("_", "") in collapsed
        for marker in _SENSITIVE_INPUT_KEY_MARKERS
    )


def _contains_reserved_input_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            not isinstance(key, str)
            or _is_reserved_input_key(key)
            or _contains_reserved_input_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_reserved_input_key(item) for item in value)
    return False


def _validate_projected_input(
    input_values: JsonDict,
    *,
    input_schema: Mapping[str, object] | None,
) -> None:
    if input_schema is None:
        return
    try:
        validate_json_schema(instance=input_values, schema=dict(input_schema))
    except (JsonSchemaValidationError, JsonSchemaSchemaError):
        raise InvocationPreflightRejectedError("invocation_input_invalid") from None


def _project_principal(
    invocation: AgentInvocation,
    *,
    claims: frozenset[InvocationPrincipalClaim],
    attribute_keys: frozenset[str],
) -> InvocationPrincipalProjection:
    attributes = {
        key: deepcopy(invocation.user.attributes[key])
        for key in attribute_keys
        if key in invocation.user.attributes
        and _principal_attribute_value_is_safe(invocation.user.attributes[key])
    }
    return InvocationPrincipalProjection(
        subject=invocation.user.id,
        tenant_id=invocation.user.tenant_id,
        roles=_project_principal_claim_values(invocation.user.roles) if "roles" in claims else None,
        groups=_project_principal_claim_values(invocation.user.groups)
        if "groups" in claims
        else None,
        entitlements=_project_principal_claim_values(invocation.user.entitlements)
        if "entitlements" in claims
        else None,
        attributes=attributes or None,
    )


def _project_principal_claim_values(values: list[str]) -> list[str]:
    return [value for value in values if _is_safe_principal_value(value)]


def _principal_attribute_value_is_safe(value: object) -> bool:
    if value is None or isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and _is_safe_principal_value(value)


def _is_safe_principal_value(value: str) -> bool:
    normalized = _INPUT_KEY_SEPARATOR_PATTERN.sub("_", value.casefold()).strip("_")
    collapsed = normalized.replace("_", "")
    return _is_logical_reference(value) and not any(
        marker in normalized or marker.replace("_", "") in collapsed
        for marker in _SENSITIVE_INPUT_KEY_MARKERS
    )


def _project_governed_context(invocation: AgentInvocation) -> InvocationContextProjection:
    """Project only stable Context locators, never content, metadata, or Trace."""

    memory: list[InvocationMemoryContextReference] = []
    knowledge: list[InvocationKnowledgeContextReference] = []
    try:
        for item in invocation.memory_context.items:
            scope = str(item.scope)
            if (
                not _is_logical_reference(item.memory_id)
                or not _is_logical_reference(scope)
                or len(scope) > 64
            ):
                raise InvocationPreflightRejectedError("invocation_context_invalid")
            memory.append(
                InvocationMemoryContextReference(
                    memory_id=item.memory_id,
                    scope=scope,
                )
            )
        for item in invocation.knowledge_context.items:
            if not _is_logical_reference(item.item_id) or not _is_logical_reference(item.source_id):
                raise InvocationPreflightRejectedError("invocation_context_invalid")
            knowledge.append(
                InvocationKnowledgeContextReference(
                    item_id=item.item_id,
                    source_id=item.source_id,
                )
            )
    except InvocationPreflightRejectedError:
        raise
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise InvocationPreflightRejectedError("invocation_context_invalid") from None
    return InvocationContextProjection(memory=memory, knowledge=knowledge)


def _is_logical_reference(value: str) -> bool:
    return is_safe_invocation_reference(value)


def _comma_separated_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _json_size(value: object) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        raise InvocationPreflightRejectedError("invocation_input_invalid") from None


def _json_size_or_none(value: object | None) -> int:
    if value is None:
        return 0
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        return -1


def _context_size(invocation: AgentInvocation) -> int:
    return _json_size(
        {
            "memory_context": invocation.memory_context.model_dump(mode="json"),
            "knowledge_context": invocation.knowledge_context.model_dump(mode="json"),
        }
    )


def _artifact_refs_are_safe(
    references: list[ArtifactRef],
    *,
    max_count: int,
    max_metadata_bytes: int,
) -> bool:
    return len(references) <= max_count and all(
        _artifact_ref_is_safe(reference, max_metadata_bytes=max_metadata_bytes)
        for reference in references
    )


def _artifact_ref_is_safe(reference: ArtifactRef, *, max_metadata_bytes: int) -> bool:
    try:
        if (
            not _is_logical_reference(reference.artifact_id)
            or not _is_logical_reference(reference.type)
            or len(reference.uri) > 2_048
            or (reference.title is not None and not _is_safe_artifact_title(reference.title))
        ):
            return False
        parsed = urlsplit(reference.uri)
        metadata = reference.metadata
        metadata_size = 0 if not metadata else _json_size_or_none(metadata)
    except (AttributeError, TypeError, ValueError):
        return False
    if not _artifact_uri_is_safe(parsed):
        return False
    if metadata_size < 0 or metadata_size > max_metadata_bytes:
        return False
    try:
        return all(
            key in _SAFE_ARTIFACT_METADATA_KEYS and _artifact_metadata_value_is_safe(key, value)
            for key, value in metadata.items()
        )
    except (AttributeError, TypeError):
        return False


def _is_safe_artifact_title(value: object) -> bool:
    """Artifact labels are locators, not a second channel for inline content."""

    return isinstance(value, str) and _is_logical_reference(value)


def _artifact_uri_is_safe(parsed) -> bool:
    """Allow only opaque Artifact locators, never a path/body side channel."""

    if (
        parsed.scheme not in {"artifact", "memory", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        # A port turns the reference into a mutable network endpoint rather
        # than a stable locator. Accessing ``port`` also safely rejects malformed
        # port syntax from an untrusted Adapter outcome.
        if parsed.port is not None:
            return False
    except ValueError:
        return False
    if not _is_safe_artifact_uri_component(parsed.netloc):
        return False
    if parsed.scheme in {"artifact", "memory"}:
        return not parsed.path
    decoded_path = _fully_unquote(parsed.path)
    if not decoded_path or decoded_path == "/":
        return True
    parts = decoded_path.removeprefix("/").split("/")
    return all(part and _is_safe_artifact_uri_component(part) for part in parts)


def _fully_unquote(value: str) -> str:
    decoded = value
    while True:
        next_value = unquote(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value


def _is_safe_artifact_uri_component(value: str) -> bool:
    normalized = _INPUT_KEY_SEPARATOR_PATTERN.sub("_", value.casefold()).strip("_")
    collapsed = normalized.replace("_", "")
    return _is_logical_reference(value) and not any(
        marker in normalized or marker.replace("_", "") in collapsed
        for marker in _SENSITIVE_INPUT_KEY_MARKERS
    )


def _artifact_metadata_value_is_safe(key: str, value: object) -> bool:
    if key == "size_bytes":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if key == "content_type":
        return isinstance(value, str) and bool(_SAFE_CONTENT_TYPE_PATTERN.fullmatch(value))
    if key == "sha256":
        return isinstance(value, str) and bool(_SAFE_SHA256_PATTERN.fullmatch(value))
    return False


async def _await_runtime_value(
    awaitable: Awaitable[_DeadlineValue],
    *,
    timeout: float,
    observe_late_task: Callable[[asyncio.Future[object]], None],
) -> _DeadlineValue:
    """Return at the deadline even if an Adapter suppresses cancellation.

    ``asyncio.wait_for`` waits for a cancelled child to finish.  Runtime
    Adapters are extension code and can accidentally catch ``CancelledError``;
    that must not let them extend the Envelope's absolute deadline.  A late
    task is cancelled and observed in the background, while Core immediately
    projects the accepted Run as a deadline failure.
    """

    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        observe_late_task(task)
        raise
    if task not in done:
        task.cancel()
        observe_late_task(task)
        raise TimeoutError
    return task.result()


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
