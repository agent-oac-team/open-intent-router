from app.core.config import Settings, get_settings
from app.dependencies import (
    get_memory_observability_service,
    get_memory_runtime_policy,
    get_memory_service,
    get_registry_service,
)
from app.main import create_app
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.repositories.memory_index_operations import MemoryIndexOutboxRepository
from app.repositories.memory_traces import MemoryFormationTraceRepository
from app.schemas.common import UserContext
from app.schemas.memory import MemoryItem, MemoryRecallRequest, MemoryWriteCandidate
from app.services.mem0_config import build_mem0_config, mem0_static_metadata
from app.services.memory_adapter import Mem0AdapterError, Mem0MemoryAdapter, _search_filter_sets
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService
from app.services.registry_service import AgentRegistryService


def test_mem0_config_uses_only_explicit_memory_fields() -> None:
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
        memory_milvus_uri="data/mem0.db",
        memory_mem0_history_database_url="postgresql+asyncpg://oir:secret@localhost:5432/oir",
        memory_embedding_base_url="https://dashscope.example/v1",
        memory_embedding_api_key="dash-key",
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

    unrelated = Settings(
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
    unrelated_config = build_mem0_config(unrelated)
    assert "openai_base_url" not in unrelated_config["embedder"]["config"]
    assert "api_key" not in unrelated_config["embedder"]["config"]
    assert "llm" not in unrelated_config


async def test_mem0_adapter_add_search_delete_and_history_traceability() -> None:
    settings = Settings(
        storage_backend="memory",
        memory_strategy_provider="mem0",
        memory_milvus_uri="data/mem0.db",
        memory_embedding_base_url="https://dashscope.example/v1",
        memory_embedding_api_key="dash-key",
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
    assert fake.adds[0]["payload"] == "prefers concise answers"
    assert fake.adds[0]["infer"] is False
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


def test_mem0_search_filter_sets_expand_multiple_scopes() -> None:
    filters = _search_filter_sets(
        MemoryRecallRequest(
            query="concise stable",
            user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
            scopes=["user_preference", "stable_fact"],
            agent_id="script_writer",
            metadata_filters={
                "source": "e2e_seed",
                "consumer": "router",
                "defer_usage_event": True,
                "request_id": "req-1",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "run_id": "run-1",
            },
        )
    )

    assert filters == [
        {
            "user_id": "u1",
            "tenant_id": "t1",
            "subject_type": "user",
            "subject_id": "u1",
            "source": "e2e_seed",
            "scope": "user_preference",
        },
        {
            "user_id": "u1",
            "tenant_id": "t1",
            "subject_type": "user",
            "subject_id": "u1",
            "source": "e2e_seed",
            "scope": "stable_fact",
        },
    ]


def test_mem0_search_filters_cannot_override_trusted_identity() -> None:
    filters = _search_filter_sets(
        MemoryRecallRequest(
            query="private",
            user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
            scopes=["stable_fact"],
            subject_id="u1",
            metadata_filters={
                "user_id": "u2",
                "tenant_id": "t2",
                "subject_type": "agent",
                "subject_id": "u2",
                "scope": "task_memory",
                "source": "trusted-source",
            },
        )
    )

    assert filters == [
        {
            "user_id": "u1",
            "tenant_id": "t1",
            "subject_type": "user",
            "subject_id": "u1",
            "source": "trusted-source",
            "scope": "stable_fact",
        }
    ]


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


async def test_mem0_adapter_uses_explicit_memory_collection_for_runtime_operations() -> None:
    settings = Settings(
        storage_backend="memory",
        memory_strategy_provider="mem0",
        memory_milvus_collection="explicit-memory-vectors",
        memory_embedding_model="explicit-memory-embedding",
        memory_embedding_dims=1536,
    )
    fake = FakeMem0Client()
    fake.vector_store = FakeVectorStore(collection_name="explicit-memory-vectors")
    repository = MemoryItemRepository()
    adapter = Mem0MemoryAdapter(settings, repository, client_factory=lambda _config: fake)

    stored = await adapter.add(
        MemoryItem(
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="explicit collection invariant",
        )
    )

    assert stored.metadata["mem0_collection"] == "explicit-memory-vectors"
    assert fake.vector_store.client.loaded_collections == ["explicit-memory-vectors"]
    assert repository.events[0].payload["collection"] == "explicit-memory-vectors"
    assert repository.events[0].payload["embedding_model"] == "explicit-memory-embedding"
    assert repository.events[0].payload["embedding_dims"] == 1536


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

    assert decisions[0].status == "accepted"
    assert decisions[0].metadata["mem0_status"] == "retryable_error"
    assert decisions[0].metadata["index_status"] == "out_of_sync"
    assert decisions[0].metadata["mem0_memory_id"] is None
    assert len(await strict_repository.list_active(tenant_id="t1")) == 1
    assert any(event.event_type == "mem0_scan" for event in strict_repository.events)
    assert not any(
        event.event_type == "memory_index_add" and event.payload["provider_status"] == "success"
        for event in strict_repository.events
    )


async def test_mem0_provider_errors_redact_uri_credentials_and_regulated_values(
    caplog,
) -> None:
    settings = Settings(
        app_env="production",
        storage_backend="memory",
        memory_strategy_provider="mem0",
    )
    repository = MemoryItemRepository()
    adapter = Mem0MemoryAdapter(
        settings,
        repository,
        client_factory=lambda _config: CredentialLeakingMem0Client(),
    )
    request = MemoryRecallRequest(
        query="private query",
        user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
    )

    try:
        await adapter.search(request)
    except Mem0AdapterError as exc:
        serialized = " ".join(
            (
                str(exc),
                str(adapter.debug_metadata()),
                str([event.payload for event in repository.events]),
                caplog.text,
            )
        )
    else:
        raise AssertionError("strict mem0 search must fail closed")

    for secret in (
        "db-user",
        "db-pass",
        "uri-secret",
        "private.person@example.test",
        "123-45-6789",
    ):
        assert secret not in serialized


async def test_mem0_missing_external_id_is_not_reported_as_provider_success() -> None:
    settings = Settings(
        app_env="production",
        storage_backend="memory",
        memory_strategy_provider="mem0",
    )
    repository = MemoryItemRepository()
    service = MemoryService(
        settings=settings,
        repository=repository,
        adapter=Mem0MemoryAdapter(
            settings,
            repository,
            client_factory=lambda _config: MissingIdMem0Client(),
        ),
    )

    decisions = await service.write_candidates(
        candidates=[MemoryWriteCandidate(scope="stable_fact", content="provider lost id")],
        user_id="u1",
        tenant_id="t1",
    )

    assert decisions[0].status == "accepted"
    assert decisions[0].metadata["mem0_status"] == "retryable_error"
    assert decisions[0].metadata["index_status"] == "out_of_sync"
    assert len(await repository.list_active(tenant_id="t1")) == 1
    assert not any(
        event.event_type == "mem0_add" and event.payload["status"] == "ok"
        for event in repository.events
    )


async def test_explicit_write_does_not_call_provider_when_canonical_commit_fails() -> None:
    settings = Settings(storage_backend="memory", memory_strategy_provider="mem0")
    repository = MemoryItemRepository()
    fake = FakeMem0Client()
    service = MemoryService(
        settings=settings,
        repository=repository,
        adapter=Mem0MemoryAdapter(
            settings,
            repository,
            client_factory=lambda _config: fake,
        ),
        lifecycle_store=FailingCanonicalStore(),
        index_outbox=MemoryIndexOutboxRepository(),
    )

    decisions = await service.write_candidates(
        candidates=[MemoryWriteCandidate(scope="stable_fact", content="canonical first")],
        user_id="u1",
        tenant_id="t1",
    )

    assert decisions[0].status == "rejected"
    assert decisions[0].reason == "memory_write_failed"
    assert fake.adds == []
    assert repository.items == {}


async def test_mem0_search_failure_is_local_fallback_or_production_fail_closed() -> None:
    request = MemoryRecallRequest(
        query="canonical",
        user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
        scopes=["stable_fact"],
    )
    local_repository = MemoryItemRepository()
    await local_repository.add(
        MemoryItem(
            memory_id="mem_local",
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="canonical fallback",
        )
    )
    local_settings = Settings(storage_backend="memory", memory_strategy_provider="mem0")
    local_service = MemoryService(
        settings=local_settings,
        repository=local_repository,
        adapter=Mem0MemoryAdapter(
            local_settings,
            local_repository,
            client_factory=lambda _config: FailingMem0Client(),
        ),
    )
    strict_repository = MemoryItemRepository()
    await strict_repository.add(
        MemoryItem(
            memory_id="mem_strict",
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="must fail closed",
        )
    )
    strict_settings = Settings(
        app_env="production",
        storage_backend="memory",
        memory_strategy_provider="mem0",
    )
    strict_service = MemoryService(
        settings=strict_settings,
        repository=strict_repository,
        adapter=Mem0MemoryAdapter(
            strict_settings,
            strict_repository,
            client_factory=lambda _config: FailingMem0Client(),
        ),
    )

    local = await local_service.recall(request)
    strict = await strict_service.recall(request)

    assert local.context.status == "ok"
    assert [item.memory_id for item in local.context.items] == ["mem_local"]
    assert local.context.metadata["mem0"]["degraded"] is True
    assert strict.context.status == "error"
    assert strict.context.items == []


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
    assert decisions[0].metadata["revision_id"].startswith("mrev_")
    assert fake.adds[0]["metadata"]["revision_id"] == decisions[0].metadata["revision_id"]
    assert fake.adds[0]["infer"] is False
    assert response.context.status == "ok"
    assert response.context.items[0].content == "prefers concise answers"
    assert response.context.metadata["mem0"]["collection"] == "oir_memory_vectors"
    assert [event.event_type for event in repository.events].count("memory_written") == 1


async def test_explicit_mem0_write_preserves_agent_source_and_safe_metadata() -> None:
    settings = Settings(storage_backend="memory", memory_strategy_provider="mem0")
    fake = FakeMem0Client()
    repository = MemoryItemRepository()
    service = MemoryService(
        settings=settings,
        repository=repository,
        adapter=Mem0MemoryAdapter(
            settings,
            repository,
            client_factory=lambda _config: fake,
        ),
    )

    decisions = await service.write_candidates(
        candidates=[
            MemoryWriteCandidate(
                scope="stable_fact",
                content="agent A private fact",
                source="explicit_api",
                agent_id="agent_a",
                metadata={"source_trace": "trace_1"},
            )
        ],
        user_id="u1",
        tenant_id="t1",
    )
    stored = await repository.get_by_id(decisions[0].memory_id, tenant_id="t1")
    agent_a = await service.recall(
        MemoryRecallRequest(
            query="private fact",
            user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
            scopes=["stable_fact"],
            agent_id="agent_a",
        )
    )
    agent_b = await service.recall(
        MemoryRecallRequest(
            query="private fact",
            user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
            scopes=["stable_fact"],
            agent_id="agent_b",
        )
    )

    assert stored is not None
    assert stored.agent_id == "agent_a"
    assert stored.source == "explicit_api"
    assert stored.metadata["source_trace"] == "trace_1"
    assert fake.adds[0]["metadata"]["agent_id"] == "agent_a"
    assert fake.adds[0]["metadata"]["source"] == "explicit_api"
    assert fake.adds[0]["metadata"]["source_trace"] == "trace_1"
    assert [item.memory_id for item in agent_a.context.items] == [stored.memory_id]
    assert agent_b.context.items == []


def test_mem0_runtime_and_debug_metadata_do_not_expose_secrets(
    non_lifespan_test_client,
) -> None:
    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        memory_strategy_provider="mem0",
        memory_milvus_token="milvus-secret",
        memory_mem0_history_database_url="postgresql+asyncpg://oir:db-secret@localhost:5432/oir",
        memory_embedding_api_key="dash-secret",
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
    formation = MemoryFormationTurnJobRepository()
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=service,
        formation_repository=formation,
        trace_repository=MemoryFormationTraceRepository(
            formation_repository=formation,
            event_repository=repository,
        ),
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_memory_runtime_policy] = lambda: settings.memory_runtime_policy
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=MemoryAgentDefinitionRepository(),
    )
    app.dependency_overrides[get_memory_service] = lambda: service
    app.dependency_overrides[get_memory_observability_service] = lambda: observability

    client = non_lifespan_test_client(app)
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
        self.updates: list[dict] = []
        self.memory_id = "mem0_ext_1"

    def add(self, payload, *, user_id: str, metadata: dict, infer: bool):
        self.adds.append(
            {
                "id": self.memory_id,
                "payload": payload,
                "user_id": user_id,
                "metadata": metadata,
                "infer": infer,
            }
        )
        return {"results": [{"id": self.memory_id, "memory": metadata.get("content", payload)}]}

    def update(self, *, memory_id: str, data: str, metadata: dict):
        self.updates.append({"memory_id": memory_id, "data": data, "metadata": metadata})
        for record in self.adds:
            if record["id"] == memory_id:
                record["payload"] = data
                record["metadata"] = metadata
        return {"message": "Memory updated successfully!"}

    def get_all(self, *, filters: dict, top_k: int):
        records = []
        for record in self.adds:
            metadata = record["metadata"]
            if all(metadata.get(key) == value for key, value in filters.items()):
                records.append(
                    {
                        "id": record["id"],
                        "memory": record["payload"],
                        "metadata": metadata,
                    }
                )
        return {"results": records[:top_k]}

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
    def add(self, payload, *, user_id: str, metadata: dict, infer: bool):
        raise RuntimeError("mem0 unavailable")

    def update(self, *, memory_id: str, data: str, metadata: dict):
        raise RuntimeError("mem0 unavailable")

    def get_all(self, *, filters: dict, top_k: int):
        raise RuntimeError("mem0 unavailable")

    def search(self, query: str, *, filters: dict, top_k: int):
        raise RuntimeError("mem0 unavailable")

    def delete(self, *, memory_id: str) -> None:
        raise RuntimeError("mem0 unavailable")


class CredentialLeakingMem0Client(FailingMem0Client):
    def search(self, query: str, *, filters: dict, top_k: int):
        raise RuntimeError(
            "postgresql://db-user:db-pass@host/db?token=uri-secret "
            "private.person@example.test 123-45-6789"
        )


class MissingIdMem0Client(FailingMem0Client):
    def add(self, payload, *, user_id: str, metadata: dict, infer: bool):
        return {"results": [{"memory": payload}]}

    def get_all(self, *, filters: dict, top_k: int):
        return {"results": []}


class FailingCanonicalStore:
    async def commit_add(self, **kwargs):
        raise RuntimeError("canonical commit failed")


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
