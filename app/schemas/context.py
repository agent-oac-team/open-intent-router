from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import JsonDict, ParticipantRole, StrictBaseModel

ContextItemSource = Literal[
    "current_input",
    "current_agent",
    "current_plan",
    "host_history",
    "agent_history",
    "recent_result",
    "recent_event",
    "artifact",
    "evidence",
    "memory",
    "knowledge",
    "frontend_context",
    "system",
]
ContextItemScope = Literal["request", "session", "agent", "plan", "user", "tenant", "global"]
ContextSelectionStatus = Literal["included", "dropped", "truncated", "summary_placeholder"]
ContextUsageSource = Literal["estimated", "provider_reported"]
ContextPurpose = Literal["route_decision", "agent_execution"]
ContextAuthority = Literal[
    "authoritative",
    "derived",
    "host_asserted",
    "model_generated",
]
ContextVisibility = Literal["router", "agent", "debug", "replay"]
ProviderStatus = Literal["ok", "empty", "skipped", "denied", "timeout", "error"]
ContextDecisionOutcome = Literal[
    "included",
    "truncated",
    "referenced",
    "budget_dropped",
    "permission_denied",
    "visibility_denied",
    "purpose_denied",
    "expired",
    "disabled",
    "duplicate_dropped",
    "overridden_for_turn",
    "superseded",
    "unresolved_conflict",
    "invalid",
]


class ContextBudget(StrictBaseModel):
    max_tokens: int = Field(gt=0)
    source_budgets: dict[str, int] = Field(default_factory=dict)
    per_item_token_limit: int | None = Field(default=None, gt=0)
    per_item_char_limit: int | None = Field(default=None, gt=0)
    chars_per_token: float = Field(default=4.0, gt=0)
    allow_summary_placeholder: bool = True


class ContextItem(StrictBaseModel):
    item_id: str = Field(min_length=1)
    source: ContextItemSource | str
    scope: ContextItemScope | str = "request"
    role: ParticipantRole | str | None = None
    content: str = ""
    structured_value: JsonDict | None = None
    priority: int = Field(default=50, ge=0, le=100)
    relevance: float = Field(default=0.5, ge=0, le=1)
    created_at: datetime | None = None
    token_estimate: int = Field(default=0, ge=0)
    char_count: int = Field(default=0, ge=0)
    included: bool = False
    status: ContextSelectionStatus = "dropped"
    drop_reason: str | None = None
    truncated: bool = False
    summary_placeholder: bool = False
    must_include: bool = False
    purpose: ContextPurpose = "route_decision"
    consumer: str = "router"
    authority: ContextAuthority = "derived"
    visibility: list[ContextVisibility | str] = Field(default_factory=list)
    source_ref: str | None = None
    dedupe_key: str | None = None
    conflict_key: str | None = None
    metadata: JsonDict = Field(default_factory=dict)


class ContextSelectionSummary(StrictBaseModel):
    item_id: str
    source: str
    scope: str
    role: str | None = None
    priority: int
    relevance: float
    token_estimate: int
    char_count: int
    included: bool
    status: ContextSelectionStatus
    drop_reason: str | None = None
    truncated: bool = False
    summary_placeholder: bool = False
    agent_id: str | None = None
    agent_session_id: str | None = None
    created_at: datetime | None = None
    metadata: JsonDict = Field(default_factory=dict)


class ContextUsage(StrictBaseModel):
    budget_tokens: int
    used_tokens: int = 0
    usage_source: ContextUsageSource = "estimated"
    included_count: int = 0
    dropped_count: int = 0
    truncated_count: int = 0
    summary_placeholder_count: int = 0
    source_distribution: dict[str, int] = Field(default_factory=dict)
    drop_reasons: dict[str, int] = Field(default_factory=dict)


class ContextPack(StrictBaseModel):
    pack_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    trace_id: str | None = None
    purpose: ContextPurpose = "route_decision"
    consumer: str = "router"
    budget: ContextBudget
    items: list[ContextItem] = Field(default_factory=list)
    selection: list[ContextSelectionSummary] = Field(default_factory=list)
    usage: ContextUsage
    policy_version: str = "m4"
    budget_version: str = "m4"
    projection_version: str = "m4"
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime | None = None


class ContextCandidate(StrictBaseModel):
    candidate_id: str = Field(min_length=1)
    source: ContextItemSource | str
    scope: ContextItemScope | str = "request"
    role: ParticipantRole | str | None = None
    content: str = ""
    structured_value: JsonDict | None = None
    purpose: ContextPurpose
    consumers: list[str] = Field(min_length=1)
    authority: ContextAuthority
    visibility: list[ContextVisibility | str] = Field(default_factory=list)
    source_ref: str | None = None
    dedupe_key: str | None = None
    conflict_key: str | None = None
    fact_type: str | None = None
    fact_value: str | int | float | bool | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    priority: int = Field(default=50, ge=0, le=100)
    relevance: float = Field(default=0.5, ge=0, le=1)
    must_include: bool = False
    permission_granted: bool = True
    enabled: bool = True
    deleted: bool = False
    expires_at: datetime | None = None
    created_at: datetime | None = None
    allowed_agent_ids: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_consumer_visibility(self) -> "ContextCandidate":
        if not any(str(consumer).strip() for consumer in self.consumers):
            raise ValueError("consumers must contain at least one non-empty consumer")
        return self


class ProviderOutcome(StrictBaseModel):
    provider: str = Field(min_length=1)
    status: ProviderStatus
    candidate_count: int = Field(default=0, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    cache_hit: bool = False
    error_code: str | None = None
    metadata: JsonDict = Field(default_factory=dict)


class ContextTraceDecision(StrictBaseModel):
    candidate_id: str
    source: str
    source_ref: str | None = None
    authority: ContextAuthority
    outcome: ContextDecisionOutcome
    reason: str | None = None
    conflict_key: str | None = None
    related_refs: list[str] = Field(default_factory=list)
    token_estimate: int = Field(default=0, ge=0)
    metadata: JsonDict = Field(default_factory=dict)


class ContextProjection(StrictBaseModel):
    projection_id: str = Field(min_length=1)
    pack_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    purpose: ContextPurpose
    consumer: str = Field(min_length=1)
    payload: JsonDict = Field(default_factory=dict)
    rendered_chars: int = Field(default=0, ge=0)
    token_estimate: int = Field(default=0, ge=0)
    projection_hash: str = ""
    projection_version: str = "v1"
    status: Literal["ok", "context_budget_exhausted"] = "ok"
    error_code: str | None = None


class ContextTrace(StrictBaseModel):
    trace_id: str = Field(min_length=1)
    pack_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    purpose: ContextPurpose
    consumer: str = Field(min_length=1)
    provider_outcomes: list[ProviderOutcome] = Field(default_factory=list)
    decisions: list[ContextTraceDecision] = Field(default_factory=list)
    budget: ContextUsage | None = None
    policy_version: str = "v1"
    budget_version: str = "v1"
    projection_version: str = "v1"
    projection_hash: str = ""
    projection_summary: JsonDict = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class ContextAssemblySession(StrictBaseModel):
    assembly_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    tenant_id: str | None = None
    provider_cache: dict[str, object] = Field(default_factory=dict)
    metadata: JsonDict = Field(default_factory=dict)


class ContextPipelineResult(StrictBaseModel):
    pack: ContextPack
    projection: ContextProjection
    trace: ContextTrace
