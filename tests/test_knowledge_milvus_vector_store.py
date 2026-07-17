from app.core.config import Settings
from app.repositories.context_stores import KnowledgeRepository
from app.schemas.common import UserContext
from app.schemas.knowledge import KnowledgeChunk, KnowledgeSearchRequest, KnowledgeSource
from app.services.knowledge_service import KnowledgeService
from app.services.knowledge_vector_store import MilvusKnowledgeVectorStore


async def test_milvus_knowledge_vector_store_hydrates_canonical_chunks() -> None:
    settings = Settings(
        storage_backend="memory",
        knowledge_vector_backend="milvus",
        knowledge_milvus_collection="oir_knowledge_vectors",
        knowledge_milvus_uri="fake-milvus.db",
        knowledge_embedding_dim=4,
    )
    repository = KnowledgeRepository()
    await repository.upsert_source(KnowledgeSource(source_id="docs", name="Docs"))
    chunk = await repository.add_chunk(
        KnowledgeChunk(
            chunk_id="chunk_risk_001",
            source_id="docs",
            content="risk rating describes product volatility",
            title="Risk Guide",
            uri="https://example.test/risk",
        )
    )
    fake_client = FakeMilvusClient()
    vector_store = MilvusKnowledgeVectorStore(
        settings,
        repository,
        client=fake_client,
        embedding_client=FakeEmbeddingClient(),
    )
    await vector_store.upsert_chunks([chunk])
    service = KnowledgeService(settings=settings, repository=repository, vector_store=vector_store)

    response = await service.search(
        KnowledgeSearchRequest(
            query="product volatility",
            user=UserContext(id="u1"),
            source_ids=["docs"],
            top_k=3,
        )
    )

    assert response.context.status == "ok"
    assert response.context.items[0].item_id == "chunk_risk_001"
    assert response.context.items[0].content == "risk rating describes product volatility"
    assert response.context.citations[0].uri == "https://example.test/risk"
    assert fake_client.created_collections[0]["primary_field_name"] == "chunk_id"
    assert fake_client.created_collections[0]["dimension"] == 4
    assert fake_client.upserted[0]["source_id"] == "docs"
    assert fake_client.searches[0]["filter"] == 'source_id in ["docs"]'
    assert fake_client.searches[0]["output_fields"] == ["chunk_id", "source_id", "title", "uri"]


async def test_milvus_knowledge_vector_store_resets_and_lists_index_records() -> None:
    settings = Settings(
        storage_backend="memory",
        knowledge_vector_backend="milvus",
        knowledge_milvus_collection="oir_knowledge_vectors",
        knowledge_milvus_uri="fake-milvus.db",
        knowledge_embedding_dim=4,
    )
    fake_client = FakeMilvusClient()
    fake_client.collection_exists = True
    fake_client.upserted = [{"chunk_id": "legacy", "source_id": "legacy"}]
    vector_store = MilvusKnowledgeVectorStore(
        settings,
        KnowledgeRepository(),
        client=fake_client,
        embedding_client=FakeEmbeddingClient(),
    )

    vector_store.reset_collection()
    await vector_store.upsert_chunks(
        [KnowledgeChunk(chunk_id="canonical-1", source_id="asset-1", content="content")]
    )

    assert fake_client.dropped_collections == ["oir_knowledge_vectors"]
    assert vector_store.list_index_records() == [
        {"chunk_id": "canonical-1", "source_id": "asset-1"}
    ]


class FakeEmbeddingClient:
    dimension = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeMilvusClient:
    def __init__(self) -> None:
        self.collection_exists = False
        self.created_collections: list[dict] = []
        self.loaded_collections: list[str] = []
        self.upserted: list[dict] = []
        self.searches: list[dict] = []
        self.dropped_collections: list[str] = []

    def has_collection(self, collection_name: str) -> bool:
        return self.collection_exists

    def create_collection(self, **kwargs) -> None:
        self.collection_exists = True
        self.created_collections.append(kwargs)

    def load_collection(self, collection_name: str) -> None:
        self.loaded_collections.append(collection_name)

    def drop_collection(self, *, collection_name: str) -> None:
        self.dropped_collections.append(collection_name)
        self.collection_exists = False
        self.upserted = []

    def upsert(self, *, collection_name: str, data: list[dict]) -> None:
        self.upserted.extend(data)

    def search(self, **kwargs) -> list[list[dict]]:
        self.searches.append(kwargs)
        source_id = _source_id_from_filter(kwargs["filter"])
        hits = [
            {
                "chunk_id": record["chunk_id"],
                "distance": 0.04,
                "entity": {
                    "chunk_id": record["chunk_id"],
                    "source_id": record["source_id"],
                    "title": record["title"],
                    "uri": record["uri"],
                },
            }
            for record in self.upserted
            if record["source_id"] == source_id
        ]
        return [hits[: kwargs["limit"]]]

    def query_iterator(self, **kwargs):
        return FakeQueryIterator(
            [
                {field: record[field] for field in kwargs["output_fields"]}
                for record in self.upserted
            ]
        )


class FakeQueryIterator:
    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.closed = False

    def next(self) -> list[dict]:
        records, self.records = self.records, []
        return records

    def close(self) -> None:
        self.closed = True


def _source_id_from_filter(filter_text: str) -> str:
    return filter_text.removeprefix('source_id in ["').removesuffix('"]')
