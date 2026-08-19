import re
from datetime import datetime
from hashlib import sha256
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import JsonDict, StrictBaseModel
from app.schemas.knowledge_persistence import sanitize_persisted_knowledge

_BINDING_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_BINDING_VERSION_FINGERPRINT_PREFIX = "oir-binding-version-sha256-"
_BINDING_VERSION_FINGERPRINT_PATTERN = re.compile(
    rf"^{_BINDING_VERSION_FINGERPRINT_PREFIX}[0-9a-f]{{64}}$"
)
_SECRET_LIKE_BINDING_VALUE_PATTERNS = (
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^AIza[A-Za-z0-9_-]{35}$"),
    re.compile(r"^ghp_[A-Za-z0-9]{36}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{22,}$"),
    re.compile(r"^glpat-[A-Za-z0-9_-]{20,}$"),
    re.compile(r"^sk-(?:live|proj)-[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^sk_(?:live|test)_[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^xoxb-[0-9A-Za-z-]{20,}$"),
)


def _is_secret_like_binding_value(value: str) -> bool:
    return any(pattern.fullmatch(value) for pattern in _SECRET_LIKE_BINDING_VALUE_PATTERNS)


def _is_safe_binding_version(value: str) -> bool:
    return bool(_BINDING_VERSION_FINGERPRINT_PATTERN.fullmatch(value))


def binding_version_fingerprint(value: str) -> str:
    """Project deployment descriptor metadata without persisting its raw value."""

    digest = sha256(f"oir-binding-version-v1:{value}".encode()).hexdigest()
    return f"{_BINDING_VERSION_FINGERPRINT_PREFIX}{digest}"


class InvocationBindingSnapshot(StrictBaseModel):
    """Bounded binding facts; version fields are opaque descriptor fingerprints."""

    schema_version: Literal["oir-binding-v1"] = "oir-binding-v1"
    kind: Literal["invocation"] = "invocation"
    adapter_key: str
    adapter_contract_version: str = Field(min_length=1, max_length=128)
    adapter_implementation_version: str = Field(min_length=1, max_length=128)
    connector_ref: str | None = None

    @field_validator("adapter_key", "connector_ref")
    @classmethod
    def require_logical_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _BINDING_IDENTIFIER_PATTERN.fullmatch(value) or _is_secret_like_binding_value(value):
            raise ValueError("binding references must be logical identifiers")
        return value

    @field_validator("adapter_contract_version", "adapter_implementation_version")
    @classmethod
    def require_safe_version(cls, value: str) -> str:
        if not _is_safe_binding_version(value):
            raise ValueError("binding versions must be bounded safe identifiers")
        return value


class AgentRun(StrictBaseModel):
    run_id: str
    request_id: str | None = None
    session_id: str
    agent_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    turn_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    status: str
    invoker_type: str
    agent_revision: int | None = Field(default=None, ge=0)
    handling_kind: Literal["invocation", "external_execution", "ui_handoff"] | None = None
    binding_snapshot: InvocationBindingSnapshot | None = None
    delegated: bool = False
    delegation_key: str | None = None
    state_version: int = Field(default=1, ge=1)
    event_sequence: int = Field(default=0, ge=0)
    deadline_at: datetime | None = None
    heartbeat_at: datetime | None = None
    claim_owner: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    terminal_event_id: str | None = None
    input: JsonDict = Field(default_factory=dict)
    output: JsonDict | None = None
    error: JsonDict | None = None
    latency_ms: int | None = None
    formation_suppressed: bool = False
    used_memory_ids: list[str] = Field(default_factory=list, max_length=50)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("input", "output", "error", mode="before")
    @classmethod
    def remove_ephemeral_knowledge_body(cls, value):
        return sanitize_persisted_knowledge(value)


class AgentResult(StrictBaseModel):
    result_id: str
    run_id: str
    session_id: str
    agent_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    turn_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    status: str
    run_state_version: int | None = Field(default=None, ge=1)
    message: str = ""
    formation_suppressed: bool = False
    formation_skip_audit_required: bool = False
    output: JsonDict | None = None
    artifact_refs: list[JsonDict] = Field(default_factory=list)
    error: JsonDict | None = None
    created_at: datetime | None = None

    @field_validator("output", "error", mode="before")
    @classmethod
    def remove_ephemeral_knowledge_body(cls, value):
        return sanitize_persisted_knowledge(value)


class RouteLog(StrictBaseModel):
    request_id: str
    session_id: str
    model_name: str
    candidate_agent_ids: list[str] = Field(default_factory=list)
    prompt_summary: str = ""
    evidence: list[JsonDict] = Field(default_factory=list)
    raw_output: JsonDict | None = None
    parsed_output: JsonDict | None = None
    validation_status: str = "ok"
    error: JsonDict | None = None
    latency_ms: int | None = None
    created_at: datetime | None = None

    @field_validator("evidence", "raw_output", "parsed_output", "error", mode="before")
    @classmethod
    def remove_ephemeral_knowledge_body(cls, value):
        return sanitize_persisted_knowledge(value)
