from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import Field

from app.schemas.agent_context import MemoryContext, MemoryContextItem, MemoryScope
from app.schemas.common import JsonDict, StrictBaseModel, UserContext

MemoryWriteStatus = Literal["accepted", "rejected", "pending", "expired"]
MemoryVisibility = Literal["user", "agent", "tenant", "system"]


class MemoryItem(StrictBaseModel):
    memory_id: str = Field(default_factory=lambda: f"mem_{uuid4().hex}")
    scope: MemoryScope | str
    subject_type: str = "user"
    subject_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    content: str
    structured_value: JsonDict = Field(default_factory=dict)
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    visibility: MemoryVisibility = "user"
    ttl_expires_at: datetime | None = None
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_context_item(self, *, relevance: float = 0.5) -> MemoryContextItem:
        return MemoryContextItem(
            memory_id=self.memory_id,
            scope=self.scope,
            content=self.content,
            relevance=relevance,
            confidence=self.confidence,
            importance=self.importance,
            source=self.source,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            ttl_expires_at=self.ttl_expires_at,
            metadata=self.metadata,
        )


class MemoryRecallRequest(StrictBaseModel):
    query: str
    user: UserContext
    scopes: list[MemoryScope | str] = Field(default_factory=list)
    agent_id: str | None = None
    subject_type: str = "user"
    subject_id: str | None = None
    max_items: int = Field(default=5, ge=0, le=50)
    metadata_filters: JsonDict = Field(default_factory=dict)


class MemoryRecallResponse(StrictBaseModel):
    context: MemoryContext = Field(default_factory=MemoryContext)
    denied_count: int = 0
    expired_count: int = 0
    metadata: JsonDict = Field(default_factory=dict)


class MemoryWriteCandidate(StrictBaseModel):
    scope: MemoryScope | str
    content: str
    source: str = "auto"
    subject_type: str = "user"
    subject_id: str | None = None
    agent_id: str | None = None
    confidence: float = Field(default=0.8, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    structured_value: JsonDict = Field(default_factory=dict)
    metadata: JsonDict = Field(default_factory=dict)


class MemoryWriteDecision(StrictBaseModel):
    candidate: MemoryWriteCandidate
    status: MemoryWriteStatus
    reason: str = ""
    memory_id: str | None = None
    metadata: JsonDict = Field(default_factory=dict)


class MemoryEvent(StrictBaseModel):
    event_id: str = Field(default_factory=lambda: f"mevt_{uuid4().hex}")
    event_type: str
    memory_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    payload: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryCleanupResult(StrictBaseModel):
    expired_memory_ids: list[str] = Field(default_factory=list)
    expired_count: int = 0


class MemoryDebugResponse(StrictBaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    events: list[MemoryEvent] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


def default_expiration_for_scope(
    scope: str,
    *,
    task_days: int,
    session_summary_days: int,
    artifact_reference_days: int,
) -> datetime | None:
    now = datetime.now(UTC)
    if scope == "task_memory":
        return now + timedelta(days=task_days)
    if scope == "session_summary":
        return now + timedelta(days=session_summary_days)
    if scope == "artifact_reference":
        return now + timedelta(days=artifact_reference_days)
    return None
