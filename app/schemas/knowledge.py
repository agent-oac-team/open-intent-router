from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from app.schemas.agent_context import KnowledgeContext, KnowledgeContextItem
from app.schemas.common import JsonDict, StrictBaseModel, UserContext

KnowledgeCallerType = Literal["router", "agent", "host", "admin"]
KnowledgePurpose = Literal["route_evidence", "agent_execution", "debug", "preview"]


class KnowledgeSource(StrictBaseModel):
    source_id: str
    name: str
    description: str = ""
    enabled: bool = True
    allow_roles: list[str] = Field(default_factory=list)
    allow_groups: list[str] = Field(default_factory=list)
    allow_tenants: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeChunk(StrictBaseModel):
    chunk_id: str = Field(default_factory=lambda: f"kchunk_{uuid4().hex}")
    source_id: str
    content: str
    title: str | None = None
    uri: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeSearchRequest(StrictBaseModel):
    query: str
    user: UserContext
    caller_type: KnowledgeCallerType = "host"
    caller_id: str | None = None
    purpose: KnowledgePurpose = "agent_execution"
    source_ids: list[str] = Field(default_factory=list)
    source_tags: list[str] = Field(default_factory=list)
    subject_type: str | None = None
    subject_id: str | None = None
    top_k: int = Field(default=5, ge=0, le=50)
    metadata: JsonDict = Field(default_factory=dict)


class KnowledgeSearchResponse(StrictBaseModel):
    context: KnowledgeContext = Field(default_factory=KnowledgeContext)
    denied_source_ids: list[str] = Field(default_factory=list)
    selected_source_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


class KnowledgeRetrievalLog(StrictBaseModel):
    log_id: str = Field(default_factory=lambda: f"klog_{uuid4().hex}")
    query: str
    caller_type: str
    caller_id: str | None = None
    purpose: str
    user_id: str | None = None
    tenant_id: str | None = None
    selected_source_ids: list[str] = Field(default_factory=list)
    denied_source_ids: list[str] = Field(default_factory=list)
    hit_count: int = 0
    status: str = "ok"
    errors: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeDebugResponse(StrictBaseModel):
    sources: list[KnowledgeSource] = Field(default_factory=list)
    chunks: list[KnowledgeChunk] = Field(default_factory=list)
    logs: list[KnowledgeRetrievalLog] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


def knowledge_item_from_chunk(chunk: KnowledgeChunk, *, score: float) -> KnowledgeContextItem:
    from app.schemas.agent_context import KnowledgeCitation

    citation = KnowledgeCitation(
        source_id=chunk.source_id,
        chunk_id=chunk.chunk_id,
        title=chunk.title,
        uri=chunk.uri,
        metadata=chunk.metadata,
    )
    return KnowledgeContextItem(
        item_id=chunk.chunk_id,
        source_id=chunk.source_id,
        content=chunk.content,
        score=score,
        title=chunk.title,
        uri=chunk.uri,
        updated_at=chunk.updated_at,
        citation=citation,
        metadata=chunk.metadata,
    )
