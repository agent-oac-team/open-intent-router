from pydantic import Field

from app.schemas.agent_context import (
    ContextStatus,
    KnowledgeCitation,
    KnowledgeContext,
    KnowledgeContextItem,
)
from app.schemas.common import JsonDict, StrictBaseModel, UserContext


class KnowledgeRetrievalBudget(StrictBaseModel):
    max_items: int = Field(default=5, ge=0, le=50)
    timeout_seconds: float | None = Field(default=None, gt=0)


class KnowledgeProviderRequest(StrictBaseModel):
    query: str
    principal: UserContext
    purpose: str
    consumer: str
    source_ids: list[str] = Field(default_factory=list)
    source_tags: list[str] = Field(default_factory=list)
    budget: KnowledgeRetrievalBudget = Field(default_factory=KnowledgeRetrievalBudget)
    trace_context: JsonDict = Field(default_factory=dict)


class KnowledgeProviderResult(StrictBaseModel):
    status: ContextStatus = "empty"
    items: list[KnowledgeContextItem] = Field(default_factory=list)
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    trace_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    truncated: bool = False
    metadata: JsonDict = Field(default_factory=dict)

    def to_context(self) -> KnowledgeContext:
        citations = self.citations or [
            item.citation for item in self.items if item.citation is not None
        ]
        metadata = dict(self.metadata)
        if self.trace_id:
            metadata["trace_id"] = self.trace_id
        if self.warnings:
            metadata["warnings"] = list(self.warnings)
        return KnowledgeContext(
            summary="\n".join(f"- {item.content}" for item in self.items),
            items=self.items,
            citations=citations,
            source_ids=list(dict.fromkeys(item.source_id for item in self.items)),
            status=self.status,
            truncated=self.truncated,
            errors=[self.error_code] if self.error_code else [],
            metadata=metadata,
        )
