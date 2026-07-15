from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import JsonDict, StrictBaseModel

ContextRetrievalMode = Literal["disabled", "prefetch", "controlled_retrieval"]
MemoryScope = Literal[
    "user_preference",
    "stable_fact",
    "task_memory",
    "artifact_reference",
    "session_summary",
]
ContextStatus = Literal["disabled", "ok", "empty", "timeout", "error", "denied"]


class ControlledRetrievalSpec(StrictBaseModel):
    query_template: str = ""
    allowed_variables: list[str] = Field(default_factory=list)
    output_key: str | None = None
    metadata: JsonDict = Field(default_factory=dict)


class AgentMemoryContextSpec(StrictBaseModel):
    mode: ContextRetrievalMode = "disabled"
    scopes: list[MemoryScope] = Field(default_factory=list)
    max_items: int = Field(default=5, ge=0, le=50)
    controlled_retrieval: ControlledRetrievalSpec | None = None
    metadata: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_memory_context(self) -> "AgentMemoryContextSpec":
        if self.mode == "disabled":
            return self
        if self.mode == "controlled_retrieval" and self.controlled_retrieval is None:
            raise ValueError(
                "controlled_retrieval config is required when memory mode is controlled_retrieval"
            )
        return self


class AgentKnowledgeContextSpec(StrictBaseModel):
    mode: ContextRetrievalMode = "disabled"
    source_ids: list[str] = Field(default_factory=list)
    source_tags: list[str] = Field(default_factory=list)
    max_items: int = Field(default=5, ge=0, le=50)
    controlled_retrieval: ControlledRetrievalSpec | None = None
    metadata: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_knowledge_context(self) -> "AgentKnowledgeContextSpec":
        if self.mode == "disabled":
            return self
        if self.mode == "controlled_retrieval" and self.controlled_retrieval is None:
            raise ValueError(
                "controlled_retrieval config is required when knowledge mode is controlled_retrieval"
            )
        return self


class AgentContextSpec(StrictBaseModel):
    memory: AgentMemoryContextSpec = Field(default_factory=AgentMemoryContextSpec)
    knowledge: AgentKnowledgeContextSpec = Field(default_factory=AgentKnowledgeContextSpec)
    metadata: JsonDict = Field(default_factory=dict)


class MemoryContextItem(StrictBaseModel):
    memory_id: str
    scope: MemoryScope | str
    content: str
    relevance: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    source: str = "memory"
    subject_type: str | None = None
    subject_id: str | None = None
    ttl_expires_at: datetime | None = None
    structured_value: JsonDict = Field(default_factory=dict)
    current_revision_id: str | None = None
    current_revision_no: int | None = Field(default=None, ge=1)
    canonical_refs: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


class MemoryContext(StrictBaseModel):
    summary: str = ""
    items: list[MemoryContextItem] = Field(default_factory=list)
    status: ContextStatus = "empty"
    truncated: bool = False
    errors: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


class KnowledgeCitation(StrictBaseModel):
    source_id: str
    chunk_id: str | None = None
    title: str | None = None
    uri: str | None = None
    metadata: JsonDict = Field(default_factory=dict)


class KnowledgeContextItem(StrictBaseModel):
    item_id: str
    source_id: str
    content: str
    score: float = Field(default=0.0, ge=0, le=1)
    title: str | None = None
    uri: str | None = None
    updated_at: datetime | None = None
    citation: KnowledgeCitation | None = None
    metadata: JsonDict = Field(default_factory=dict)


class KnowledgeContext(StrictBaseModel):
    summary: str = ""
    items: list[KnowledgeContextItem] = Field(default_factory=list)
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    status: ContextStatus = "disabled"
    truncated: bool = False
    errors: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


class AgentRuntimeContext(StrictBaseModel):
    memory_context: MemoryContext = Field(default_factory=MemoryContext)
    knowledge_context: KnowledgeContext = Field(default_factory=KnowledgeContext)
    metadata: JsonDict = Field(default_factory=dict)
