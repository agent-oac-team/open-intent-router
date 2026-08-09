from typing import Protocol, runtime_checkable

from app.schemas.knowledge_provider import (
    KnowledgeProviderRequest,
    KnowledgeProviderResult,
)


@runtime_checkable
class KnowledgeProvider(Protocol):
    async def retrieve(self, request: KnowledgeProviderRequest) -> KnowledgeProviderResult: ...
