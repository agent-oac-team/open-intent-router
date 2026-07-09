from typing import Protocol

from app.core.config import Settings
from app.repositories.context_stores import KnowledgeRepository
from app.schemas.knowledge import KnowledgeChunk


class KnowledgeVectorStore(Protocol):
    async def search(
        self,
        *,
        query: str,
        source_ids: list[str],
        limit: int,
    ) -> list[tuple[KnowledgeChunk, float]]: ...


class RepositoryKnowledgeVectorStore:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    async def search(
        self,
        *,
        query: str,
        source_ids: list[str],
        limit: int,
    ) -> list[tuple[KnowledgeChunk, float]]:
        return await self.repository.search_chunks(query=query, source_ids=source_ids, limit=limit)


class MilvusKnowledgeVectorStore:
    def __init__(self, settings: Settings, repository: KnowledgeRepository) -> None:
        self.settings = settings
        self.repository = repository

    async def search(
        self,
        *,
        query: str,
        source_ids: list[str],
        limit: int,
    ) -> list[tuple[KnowledgeChunk, float]]:
        try:
            import pymilvus  # noqa: F401
        except Exception as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError(
                "pymilvus is not installed; cannot use Milvus vector backend"
            ) from exc
        # Embedding generation and collection-specific vector search are deployment concerns.
        # Until an embedding provider is configured, hydrate through the repository fallback.
        return await self.repository.search_chunks(query=query, source_ids=source_ids, limit=limit)


def build_knowledge_vector_store(
    settings: Settings,
    repository: KnowledgeRepository,
) -> KnowledgeVectorStore:
    if settings.knowledge_vector_backend == "milvus":
        return MilvusKnowledgeVectorStore(settings, repository)
    return RepositoryKnowledgeVectorStore(repository)
