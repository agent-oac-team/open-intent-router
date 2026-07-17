from typing import Any, Protocol

import httpx

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
    def __init__(
        self,
        settings: Settings,
        repository: KnowledgeRepository,
        *,
        client: Any | None = None,
        embedding_client: "OpenAICompatibleEmbeddingClient | None" = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._client = client
        self.embedding_client = embedding_client or OpenAICompatibleEmbeddingClient(settings)

    async def upsert_chunks(self, chunks: list[KnowledgeChunk]) -> int:
        if not chunks:
            return 0
        client = self._milvus_client()
        self._ensure_collection(client)
        vectors = await self.embedding_client.embed([chunk.content for chunk in chunks])
        records = [
            {
                "chunk_id": chunk.chunk_id,
                "source_id": chunk.source_id,
                "title": chunk.title or "",
                "uri": chunk.uri or "",
                "vector": vector,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        client.upsert(collection_name=self.settings.knowledge_milvus_collection, data=records)
        self._load_collection(client)
        return len(records)

    def reset_collection(self) -> None:
        client = self._milvus_client()
        if client.has_collection(self.settings.knowledge_milvus_collection):
            client.drop_collection(collection_name=self.settings.knowledge_milvus_collection)
        self._ensure_collection(client)

    def list_index_records(self, *, batch_size: int = 500) -> list[dict[str, Any]]:
        client = self._milvus_client()
        self._ensure_collection(client)
        iterator = client.query_iterator(
            collection_name=self.settings.knowledge_milvus_collection,
            filter="",
            batch_size=batch_size,
            output_fields=["chunk_id", "source_id"],
        )
        records: list[dict[str, Any]] = []
        try:
            while batch := iterator.next():
                records.extend(record for record in batch if isinstance(record, dict))
        finally:
            iterator.close()
        return records

    async def search(
        self,
        *,
        query: str,
        source_ids: list[str],
        limit: int,
    ) -> list[tuple[KnowledgeChunk, float]]:
        if limit <= 0:
            return []
        client = self._milvus_client()
        self._ensure_collection(client)
        query_vector = (await self.embedding_client.embed([query]))[0]
        results = client.search(
            collection_name=self.settings.knowledge_milvus_collection,
            data=[query_vector],
            limit=limit,
            filter=_source_filter(source_ids),
            output_fields=["chunk_id", "source_id", "title", "uri"],
        )
        hits = results[0] if results else []
        scored_ids = [
            (chunk_id, _hit_score(hit))
            for hit in hits
            if (chunk_id := _hit_chunk_id(hit)) is not None
        ]
        chunks = await self.repository.get_chunks_by_ids([chunk_id for chunk_id, _ in scored_ids])
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        return [
            (chunk_by_id[chunk_id], score)
            for chunk_id, score in scored_ids
            if chunk_id in chunk_by_id
        ]

    def _milvus_client(self):
        if self._client is not None:
            return self._client
        if not self.settings.knowledge_milvus_uri:
            raise RuntimeError("KNOWLEDGE_MILVUS_URI is required for Milvus knowledge backend")
        try:
            from pymilvus import MilvusClient
        except Exception as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError(
                "pymilvus is not installed; cannot use Milvus knowledge vector backend"
            ) from exc
        kwargs: dict[str, Any] = {}
        if self.settings.knowledge_milvus_token:
            kwargs["token"] = self.settings.knowledge_milvus_token
        if self.settings.knowledge_milvus_db_name:
            kwargs["db_name"] = self.settings.knowledge_milvus_db_name
        self._client = MilvusClient(uri=self.settings.knowledge_milvus_uri, **kwargs)
        return self._client

    def _ensure_collection(self, client: Any) -> None:
        if client.has_collection(self.settings.knowledge_milvus_collection):
            self._load_collection(client)
            return
        client.create_collection(
            collection_name=self.settings.knowledge_milvus_collection,
            dimension=self.embedding_client.dimension,
            primary_field_name="chunk_id",
            id_type="string",
            vector_field_name="vector",
            metric_type="COSINE",
            auto_id=False,
        )
        self._load_collection(client)

    def _load_collection(self, client: Any) -> None:
        try:
            client.load_collection(collection_name=self.settings.knowledge_milvus_collection)
        except TypeError:
            client.load_collection(self.settings.knowledge_milvus_collection)


class OpenAICompatibleEmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.knowledge_embedding_model or settings.embedding_model
        self.base_url = settings.knowledge_embedding_base_url or settings.embedding_base_url
        self.api_key = settings.knowledge_embedding_api_key or settings.embedding_api_key
        self.dimension = settings.knowledge_embedding_dim or settings.embedding_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.base_url:
            raise RuntimeError("KNOWLEDGE_EMBEDDING_BASE_URL or EMBEDDING_BASE_URL is required")
        if not self.api_key:
            raise RuntimeError("KNOWLEDGE_EMBEDDING_API_KEY or EMBEDDING_API_KEY is required")
        url = self.base_url.rstrip("/") + "/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": self.model, "input": texts}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=headers, json=body)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"knowledge embedding request failed: {exc}") from exc
        data = response.json()
        try:
            embeddings = [item["embedding"] for item in data["data"]]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("knowledge embedding response is missing data[].embedding") from exc
        for vector in embeddings:
            if len(vector) != self.dimension:
                raise RuntimeError(
                    "knowledge embedding dimension mismatch: "
                    f"expected {self.dimension}, got {len(vector)}"
                )
        return embeddings


def _source_filter(source_ids: list[str]) -> str:
    if not source_ids:
        return ""
    values = ", ".join(_milvus_string(value) for value in source_ids)
    return f"source_id in [{values}]"


def _milvus_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _hit_chunk_id(hit: Any) -> str | None:
    if not isinstance(hit, dict):
        return None
    for key in ("chunk_id", "id"):
        value = hit.get(key)
        if isinstance(value, str):
            return value
    entity = hit.get("entity")
    if isinstance(entity, dict):
        value = entity.get("chunk_id")
        if isinstance(value, str):
            return value
    return None


def _hit_score(hit: Any) -> float:
    if not isinstance(hit, dict):
        return 0.0
    score = hit.get("score")
    if isinstance(score, int | float):
        return max(0.0, min(float(score), 1.0))
    distance = hit.get("distance")
    if isinstance(distance, int | float):
        return max(0.0, min(1.0 - float(distance), 1.0))
    return 0.0


def build_knowledge_vector_store(
    settings: Settings,
    repository: KnowledgeRepository,
) -> KnowledgeVectorStore:
    if settings.knowledge_vector_backend == "milvus":
        return MilvusKnowledgeVectorStore(settings, repository)
    return RepositoryKnowledgeVectorStore(repository)
