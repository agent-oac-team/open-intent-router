from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.logs import (
    is_external_execution_binding_fingerprint,
    is_safe_external_executor_reference,
)

ExecutionTraceEventType = Literal[
    "canonical_turn",
    "context_pack",
    "route_decision",
    "agent_run",
    "agent_event",
    "agent_result",
    "ui_handoff",
    "memory_recall",
    "memory_formation",
    "memory_decision",
    "memory_revision",
    "trace_integrity",
]
TraceCompleteness = Literal["complete", "incomplete"]
TraceVisibility = Literal["business_runtime", "technical"]

_FACT_KEYS: dict[str, frozenset[str]] = {
    "canonical_turn": frozenset({"request_id", "source", "input_kind", "outcome"}),
    "context_pack": frozenset(
        {"consumer", "included_count", "excluded_count", "degraded", "reason_code"}
    ),
    "route_decision": frozenset(
        {"action", "target_agent_id", "reason", "candidate_count", "plan_id", "capability"}
    ),
    "agent_run": frozenset(
        {
            "agent_id",
            "capability",
            "invoker_type",
            "delegated",
            "agent_revision",
            "handling_kind",
            "binding_schema_version",
            "adapter_contract_version",
            "adapter_implementation_version",
            "executor_ref",
            "executor_binding_id",
        }
    ),
    "agent_event": frozenset(
        {
            "agent_id",
            "capability",
            "provider_stage_name",
            "result_summary",
            "error_code",
        }
    ),
    "agent_result": frozenset(
        {"agent_id", "capability", "result_summary", "artifact_count", "error_code"}
    ),
    "ui_handoff": frozenset({"target_route", "from_route", "reason", "failure_code"}),
    "memory_recall": frozenset(
        {"scope", "used_count", "excluded_count", "degraded", "reason_code"}
    ),
    "memory_formation": frozenset({"candidate_count", "formation_status", "reason_code", "job_id"}),
    "memory_decision": frozenset(
        {
            "decision_id",
            "operation",
            "decision_status",
            "reason_code",
            "previous_value",
            "proposed_value",
            "revision_id",
        }
    ),
    "memory_revision": frozenset(
        {
            "memory_id",
            "revision_id",
            "operation",
            "index_status",
            "index_operation_status",
        }
    ),
    "trace_integrity": frozenset({"reason_code", "failed_source", "failure_id", "recovered"}),
}

_FORBIDDEN_FACT_KEY_FRAGMENTS = frozenset(
    {
        "authorization",
        "credential",
        "password",
        "payload",
        "prompt",
        "secret",
        "stack",
        "token",
    }
)


class TraceEvidenceRef(BaseModel):
    reference_type: str = Field(min_length=1, max_length=64)
    reference_id: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=256)


class _ExecutionTraceEventEnvelope(BaseModel):
    # A Trace ID is derived as ``trace_<turn_id>``; Canonical Turn IDs permit 128 characters.
    trace_id: str = Field(min_length=1, max_length=134)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    event_type: ExecutionTraceEventType
    stage: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=256)
    source_version: int = Field(default=1, ge=1, le=1_000_000)
    reason_code: str | None = Field(default=None, max_length=128)
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[TraceEvidenceRef] = Field(default_factory=list, max_length=20)
    visibility: TraceVisibility = "business_runtime"
    schema_version: int = Field(default=1, ge=1, le=1_000_000)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @field_validator("facts")
    @classmethod
    def validate_fact_shape(cls, facts: dict[str, Any]) -> dict[str, Any]:
        if len(facts) > 12:
            raise ValueError("facts contains too many fields")
        return facts

    @model_validator(mode="after")
    def validate_facts_for_event_type(self) -> "_ExecutionTraceEventEnvelope":
        allowed = _FACT_KEYS[self.event_type]
        unknown = set(self.facts) - allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"facts contains unsupported fields for {self.event_type}: {names}")
        for value in self.facts.values():
            _validate_fact_value(value)
        binding_id = self.facts.get("executor_binding_id")
        if binding_id is not None and not is_external_execution_binding_fingerprint(binding_id):
            raise ValueError(
                "executor_binding_id must be a non-reversible external binding fingerprint"
            )
        executor_ref = self.facts.get("executor_ref")
        if executor_ref is not None and not is_safe_external_executor_reference(executor_ref):
            raise ValueError("executor_ref must be a logical binding identifier")
        return self


class ExecutionTraceEventDraft(_ExecutionTraceEventEnvelope):
    schema_version: int = Field(default=2, ge=2, le=1_000_000)

    @model_validator(mode="after")
    def reject_memory_body_values(self) -> "ExecutionTraceEventDraft":
        if self.event_type == "memory_decision":
            if {"previous_value", "proposed_value"} & self.facts.keys():
                raise ValueError("memory decision body values cannot be written to a trace")
        return self


class ExecutionTraceEvent(_ExecutionTraceEventEnvelope):
    event_offset: int = Field(ge=1)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def restrict_memory_body_values_to_legacy_schema(self) -> "ExecutionTraceEvent":
        if self.schema_version >= 2 and self.event_type == "memory_decision":
            if {"previous_value", "proposed_value"} & self.facts.keys():
                raise ValueError("memory decision body values require schema version 1")
        return self

    @field_validator("recorded_at")
    @classmethod
    def normalize_recorded_at(cls, value: datetime) -> datetime:
        return _as_utc(value)


class ExecutionTraceQuery(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)


class ExecutionTraceRecoveredState(BaseModel):
    source: Literal["canonical_turn"] = "canonical_turn"
    status: str = Field(min_length=1, max_length=64)
    outcome: str | None = Field(default=None, max_length=64)
    state_version: int = Field(ge=1)


class ExecutionTraceRecoveredMemoryRevision(BaseModel):
    source: Literal["canonical_memory"] = "canonical_memory"
    memory_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    index_status: str = Field(min_length=1, max_length=64)
    index_operation_status: str | None = Field(default=None, max_length=64)


class ExecutionTraceSnapshot(BaseModel):
    trace_id: str | None = None
    events: list[ExecutionTraceEvent] = Field(default_factory=list)
    watermark: int = Field(default=0, ge=0)
    completeness: TraceCompleteness = "complete"
    incomplete_reason_codes: list[str] = Field(default_factory=list, max_length=20)
    recovered: bool = False
    recovered_state: ExecutionTraceRecoveredState | None = None
    recovered_memory_revisions: list[ExecutionTraceRecoveredMemoryRevision] = Field(
        default_factory=list,
        max_length=20,
    )


class ExecutionTraceWriteResult(BaseModel):
    event: ExecutionTraceEvent
    created: bool
    completeness: TraceCompleteness = "complete"


def trace_id_for_turn(turn_id: str) -> str:
    return f"trace_{turn_id}"


def _validate_fact_value(value: Any, *, depth: int = 0) -> None:
    if depth > 2:
        raise ValueError("facts nesting exceeds the allowed depth")
    if value is None or isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        if len(value) > 512:
            raise ValueError("facts string value exceeds the allowed length")
        return
    if isinstance(value, list):
        if len(value) > 20:
            raise ValueError("facts list exceeds the allowed length")
        for item in value:
            _validate_fact_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 12:
            raise ValueError("facts object contains too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 64:
                raise ValueError("facts object keys must be bounded strings")
            normalized_key = key.lower()
            if any(fragment in normalized_key for fragment in _FORBIDDEN_FACT_KEY_FRAGMENTS):
                raise ValueError("facts object contains a prohibited field")
            _validate_fact_value(item, depth=depth + 1)
        return
    raise ValueError("facts values must be bounded scalars, lists, or objects")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
