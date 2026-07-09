from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.dependencies import get_memory_service, get_registry_service
from app.main import create_app
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.schemas.common import UserContext
from app.schemas.memory import MemoryItem, MemoryRecallRequest, MemoryWriteCandidate
from app.services.mem0_config import build_mem0_config, mem0_static_metadata
from app.services.memory_adapter import Mem0MemoryAdapter
from app.services.memory_service import MemoryService
from app.services.registry_service import AgentRegistryService


def test_mem0_config_defaults_explicit_fields_and_json_override() -> None:
    defaults = Settings(storage_backend="memory")
    default_config = build_mem0_config(defaults)

    assert defaults.memory_strategy_provider == "memory"
    assert defaults.memory_mem0_fail_closed_effective is False
    assert default_config["vector_store"]["provider"] == "milvus"
    assert default_config["vector_store"]["config"]["collection_name"] == "oir_memory_vectors"
    assert default_config["vector_store"]["config"]["embedding_model_dims"] == 1024
    assert default_config["vector_store"]["config"]["token"] == ""
    assert default_config["embedder"]["config"]["model"] == "text-embedding-v4"
    assert default_config["history_db_path"] == "./data/mem0-history.db"

    explicit = Settings(
        app_env="production",
        storage_backend="memory",
        memory_strategy_provider="mem0",
        memory_mem0_milvus_uri="data/mem0.db",
        memory_mem0_history_database_url="postgresql+asyncpg://oir:secret@localhost:5432/oir",
        knowledge_embedding_base_url="https://dashscope.example/v1",
        knowledge_embedding_api_key="dash-key",
        memory_mem0_llm_model="qwen-plus",
        memory_mem0_llm_api_key="llm-key",
    )
    explicit_config = build_mem0_config(explicit)
    metadata = mem0_static_metadata(explicit)

    assert explicit.memory_mem0_fail_closed_effective is True
    assert explicit_config["vector_store"]["config"]["url"] == "data/mem0.db"
    assert (
        explicit_config["embedder"]["config"]["openai_base_url"] == "https://dashscope.example/v1"
    )
    assert explicit_config["embedder"]["config"]["api_key"] == "dash-key"
    assert explicit_config["llm"]["config"]["model"] == "qwen-plus"
    assert metadata["history_backend"] == "postgresql"
    assert metadata["history_canonical"] == "oir_memory_events_ledger"

    override = Settings(
        storage_backend="memory",
        mem0_config_json='{"vector_store":{"config":{"collection_name":"custom_memories"}}}',
    )
    override_config = build_mem0_config(override)
    assert override_config["vector_store"]["config"]["collection_name"] == "custom_memories"
    assert override_config["vector_store"]["config"]["url"] == ".data/oir_memory_milvus.db"

    shorthand = Settings(
        storage_backend="memory",
        router_llm_provider="openai_compatible",
        router_llm_model="deepseek-chat",
        router_llm_base_url="https://api.deepseek.com",
        router_llm_api_key="deepseek-key",
        embedding_base_url="https://dashscope.example/v1",
        embedding_api_key="dash-key",
        embedding_model="text-embedding-v4",
        embedding_dim=1024,
    )
    shorthand_config = build_mem0_config(shorthand)
    assert (
        shorthand_config["embedder"]["config"]["openai_base_url"] == "https://dashscope.example/v1"
    )
    assert shorthand_config["embedder"]["config"]["api_key"] == "dash-key"
    assert shorthand_config["llm"]["config"]["model"] == "deepseek-chat"
    assert shorthand_config["llm"]["config"]["openai_base_url"] == "https://api.deepseek.com"
    assert shorthand_config["llm"]["config"]["api_key"] == "deepseek-key"


async def test_mem0_adapter_add_search_delete_and_history_traceability() -> None:
    settings = Settings(
        storage_backend="memory",
        memory_strategy_provider="mem0",
        memory_mem0_milvus_uri="data/mem0.db",
        knowledge_embedding_base_url="https://dashscope.example/v1",
        knowledge_embedding_api_key="dash-key",
    )
    fake = FakeMem0Client()
    repository = MemoryItemRepository()
    adapter = Mem0MemoryAdapter(settings, repository, client_factory=lambda _config: fake)

    item = MemoryItem(
        memory_id="mem_oir_1",
        scope="user_preference",
        subject_id="u1",
        user_id="u1",
        tenant_id="t1",
        agent_id="agent_a",
        content="prefers concise answers",
        source="message",
        metadata={"source_trace": "test"},
    )

    stored = await adapter.add(item)
    recalled = await adapter.search(
        MemoryRecallRequest(
            query="concise",
            user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
            scopes=["user_preference"],
            agent_id="agent_a",
            max_items=3,
        )
    )
    await adapter.delete_many([stored.memory_id], items=[stored])

    assert fake.adds[0]["metadata"]["memory_id"] == "mem_oir_1"
    assert fake.adds[0]["metadata"]["scope"] == "user_preference"
    assert stored.metadata["mem0_memory_id"] == "mem0_ext_1"
    assert recalled[0].memory_id == "mem_oir_1"
    assert recalled[0].metadata["mem0_memory_id"] == "mem0_ext_1"
    assert fake.searches[0]["filters"]["user_id"] == "u1"
    assert fake.searches[0]["filters"]["tenant_id"] == "t1"
    assert fake.deletes == ["mem0_ext_1"]
    assert [event.event_type for event in repository.events] == [
        "mem0_add",
        "mem0_search",
        "mem0_delete",
    ]
    assert repository.events[0].payload["collection"] == "oir_memory_vectors"
    assert repository.events[0].payload["history_canonical"] == "oir_memory_events_ledger"


async def test_mem0_adapter_loads_milvus_collection_after_client_init() -> None:
    settings = Settings(storage_backend="memory", memory_strategy_provider="mem0")
    fake = FakeMem0Client()
    fake.vector_store = FakeVectorStore(collection_name="oir_memory_vectors")
    repository = MemoryItemRepository()
    adapter = Mem0MemoryAdapter(settings, repository, client_factory=lambda _config: fake)

    await adapter.add(
        MemoryItem(
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            content="loads milvus collection",
        )
    )

    assert fake.vector_store.client.loaded_collections == ["oir_memory_vectors"]


async def test_mem0_adapter_patches_milvus_lite_star_output_fields() -> None:
    settings = Settings(storage_backend="memory", memory_strategy_provider="mem0")
    fake = FakeMem0Client()
    fake.vector_store = FakeVectorStore(collection_name="oir_memory_vectors")
    repository = MemoryItemRepository()
    adapter = Mem0MemoryAdapter(settings, repository, client_factory=lambda _config: fake)

    await adapter.add(
        MemoryItem(
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            content="patches milvus output fields",
        )
    )
    fake.vector_store.client.search(collection_name="oir_memory_vectors", output_fields=["*"])

    assert fake.vector_store.client.search_output_fields == [["id", "metadata"]]


async def test_memory_service_policy_happens_before_adapter_extract() -> None:
    adapter = CountingAdapter()
    service = MemoryService(
        settings=Settings(storage_backend="memory"),
        repository=MemoryItemRepository(),
        adapter=adapter,
    )

    decisions = await service.write_candidates(
        candidates=[
            MemoryWriteCandidate(scope="stable_fact", content="too weak", confidence=0.2),
            MemoryWriteCandidate(scope="stable_fact", content="accepted fact", confidence=0.9),
        ],
        user_id="u1",
        tenant_id="t1",
    )

    assert [decision.status for decision in decisions] == ["rejected", "accepted"]
    assert adapter.extract_calls == 1
    assert adapter.add_calls == 1


async def test_mem0_fail_closed_rejects_write_and_local_fallback_is_degraded() -> None:
    local_settings = Settings(storage_backend="memory", memory_strategy_provider="mem0")
    local_repository = MemoryItemRepository()
    local_adapter = Mem0MemoryAdapter(
        local_settings,
        local_repository,
        client_factory=lambda _config: FailingMem0Client(),
    )
    local_stored = await local_adapter.add(
        MemoryItem(scope="stable_fact", subject_id="u1", user_id="u1", content="fallback fact")
    )

    assert local_stored.metadata["memory_provider"] == "repository_fallback"
    assert local_adapter.debug_metadata()["degraded"] is True
    assert any(event.payload["status"] == "fallback" for event in local_repository.events)

    strict_settings = Settings(
        app_env="production",
        storage_backend="memory",
        memory_strategy_provider="mem0",
    )
    strict_repository = MemoryItemRepository()
    strict_service = MemoryService(
        settings=strict_settings,
        repository=strict_repository,
        adapter=Mem0MemoryAdapter(
            strict_settings,
            strict_repository,
            client_factory=lambda _config: FailingMem0Client(),
        ),
    )

    decisions = await strict_service.write_candidates(
        candidates=[MemoryWriteCandidate(scope="stable_fact", content="strict fact")],
        user_id="u1",
        tenant_id="t1",
    )

    assert decisions[0].status == "rejected"
    assert decisions[0].reason == "memory_write_failed"
    assert any(event.event_type == "mem0_add" for event in strict_repository.events)
    assert any(event.event_type == "memory_write_failed" for event in strict_repository.events)


async def test_memory_service_mem0_closed_loop_recall_context() -> None:
    settings = Settings(storage_backend="memory", memory_strategy_provider="mem0")
    fake = FakeMem0Client()
    repository = MemoryItemRepository()
    adapter = Mem0MemoryAdapter(settings, repository, client_factory=lambda _config: fake)
    service = MemoryService(settings=settings, repository=repository, adapter=adapter)

    decisions = await service.write_candidates(
        candidates=[
            MemoryWriteCandidate(
                scope="user_preference",
                content="prefers concise answers",
                confidence=0.9,
                source="message",
            )
        ],
        user_id="u1",
        tenant_id="t1",
    )
    response = await service.recall(
        MemoryRecallRequest(
            query="concise",
            user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
            scopes=["user_preference"],
        )
    )

    assert decisions[0].status == "accepted"
    assert decisions[0].metadata["mem0_memory_id"] == "mem0_ext_1"
    assert response.context.status == "ok"
    assert response.context.items[0].content == "prefers concise answers"
    assert response.context.metadata["mem0"]["collection"] == "oir_memory_vectors"
    assert [event.event_type for event in repository.events].count("memory_written") == 1


def test_mem0_runtime_and_debug_metadata_do_not_expose_secrets() -> None:
    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        memory_strategy_provider="mem0",
        memory_mem0_milvus_token="milvus-secret",
        memory_mem0_history_database_url="postgresql+asyncpg://oir:db-secret@localhost:5432/oir",
        knowledge_embedding_api_key="dash-secret",
        memory_mem0_llm_api_key="llm-secret",
    )
    repository = MemoryItemRepository()
    service = MemoryService(
        settings=settings,
        repository=repository,
        adapter=Mem0MemoryAdapter(
            settings, repository, client_factory=lambda _config: FakeMem0Client()
        ),
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=MemoryAgentDefinitionRepository(),
    )
    app.dependency_overrides[get_memory_service] = lambda: service

    client = TestClient(app)
    runtime = client.get("/api/v1/runtime/config").json()
    debug = client.get("/api/v1/memories/debug").json()
    serialized = f"{runtime} {debug}"

    assert runtime["memory_mem0_collection"] == "oir_memory_vectors"
    assert debug["metadata"]["mem0"]["collection"] == "oir_memory_vectors"
    assert "milvus-secret" not in serialized
    assert "db-secret" not in serialized
    assert "dash-secret" not in serialized
    assert "llm-secret" not in serialized


class FakeMem0Client:
    def __init__(self) -> None:
        self.adds: list[dict] = []
        self.searches: list[dict] = []
        self.deletes: list[str] = []
        self.memory_id = "mem0_ext_1"

    def add(self, payload, *, user_id: str, metadata: dict):
        self.adds.append({"payload": payload, "user_id": user_id, "metadata": metadata})
        return {"results": [{"id": self.memory_id, "memory": metadata.get("content", payload)}]}

    def search(self, query: str, *, filters: dict, top_k: int):
        self.searches.append({"query": query, "filters": filters, "top_k": top_k})
        metadata = self.adds[-1]["metadata"] if self.adds else {}
        return {
            "results": [
                {
                    "id": self.memory_id,
                    "memory": self.adds[-1]["payload"] if self.adds else "memory",
                    "score": 0.91,
                    "metadata": metadata,
                }
            ]
        }

    def delete(self, *, memory_id: str) -> None:
        self.deletes.append(memory_id)


class FailingMem0Client:
    def add(self, payload, *, user_id: str, metadata: dict):
        raise RuntimeError("mem0 unavailable")

    def search(self, query: str, *, filters: dict, top_k: int):
        raise RuntimeError("mem0 unavailable")

    def delete(self, *, memory_id: str) -> None:
        raise RuntimeError("mem0 unavailable")


class FakeMilvusClient:
    def __init__(self) -> None:
        self.loaded_collections: list[str] = []
        self.search_output_fields: list[list[str] | str | None] = []

    def load_collection(self, *, collection_name: str) -> None:
        self.loaded_collections.append(collection_name)

    def search(self, **kwargs):
        self.search_output_fields.append(kwargs.get("output_fields"))
        return []


class FakeVectorStore:
    def __init__(self, collection_name: str) -> None:
        self.collection_name = collection_name
        self.client = FakeMilvusClient()


class CountingAdapter:
    def __init__(self) -> None:
        self.extract_calls = 0
        self.add_calls = 0

    async def search(self, request: MemoryRecallRequest) -> list[MemoryItem]:
        return []

    async def add(self, item: MemoryItem) -> MemoryItem:
        self.add_calls += 1
        return item

    async def extract(self, candidates: list[MemoryWriteCandidate]) -> list[MemoryWriteCandidate]:
        self.extract_calls += 1
        return candidates

    async def delete_many(
        self,
        memory_ids: list[str],
        *,
        items: list[MemoryItem] | None = None,
    ) -> None:
        return None
