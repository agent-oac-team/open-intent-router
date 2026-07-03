from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import JsonDict, ParticipantRole, StrictBaseModel


ContextItemSource = Literal[
    "current_input",
    "current_agent",
    "current_plan",
    "host_history",
    "agent_history",
    "recent_result",
    "recent_event",
    "evidence",
    "memory",
    "frontend_context",
    "system",
]
ContextItemScope = Literal["request", "session", "agent", "plan", "user", "tenant", "global"]
ContextSelectionStatus = Literal["included", "dropped", "truncated", "summary_placeholder"]
ContextUsageSource = Literal["estimated", "provider_reported"]


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
    budget: ContextBudget
    items: list[ContextItem] = Field(default_factory=list)
    selection: list[ContextSelectionSummary] = Field(default_factory=list)
    usage: ContextUsage
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime | None = None
