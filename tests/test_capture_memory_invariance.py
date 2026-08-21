import json

from app.core.config import Settings, get_settings
from app.dependencies import (
    get_memory_management_service,
    get_memory_observability_service,
    get_memory_runtime_policy,
    get_memory_service,
    get_registry_service,
)
from app.main import create_app
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.repositories.memory_traces import MemoryFormationTraceRepository
from app.services.memory_management import MemoryManagementService
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService
from app.services.registry_service import AgentRegistryService
from scripts.capture_memory_invariance import capture_memory_invariance


def test_capture_memory_invariance_uses_public_crud_and_omits_memory_body(
    non_lifespan_test_client,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="local",
        storage_backend="memory",
        registry_backend="database",
        memory_mode="on",
        memory_identity_secret="baseline-secret",
        memory_database_url="sqlite+aiosqlite:///explicit-memory.db",
        memory_milvus_uri=".data/oir_memory_milvus.db",
        memory_milvus_collection="oir_memory_vectors",
        memory_embedding_model="text-embedding-v4",
        memory_embedding_dims=1024,
    )
    repository = MemoryItemRepository()
    memory = MemoryService(settings=settings, repository=repository)
    formation = MemoryFormationTurnJobRepository()
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory,
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
    app.dependency_overrides[get_memory_service] = lambda: memory
    app.dependency_overrides[get_memory_management_service] = lambda: MemoryManagementService(
        memory_service=memory
    )
    app.dependency_overrides[get_memory_observability_service] = lambda: observability

    report = capture_memory_invariance(
        non_lifespan_test_client(app),
        tenant_id="tenant-baseline",
        user_id="user-baseline",
        recall_query="representative baseline query",
        identity_secret="baseline-secret",
        exercise_crud=True,
        probe_content="SENTINEL memory body must never enter report",
    )

    serialized = json.dumps(report, ensure_ascii=False)
    assert report["schema_version"] == "oir-memory-invariance-v1"
    assert report["effective_configuration"]["memory_database_source"] == "MEMORY_DATABASE_URL"
    assert report["storage"]["collection"] == "oir_memory_vectors"
    assert report["storage"]["active_item_count_before"] == 0
    assert report["storage"]["active_item_count_after"] == 0
    assert report["crud_probe"]["write_status"] == "accepted"
    assert report["crud_probe"]["recall_found"] is True
    assert report["crud_probe"]["delete_status"] in {"pending", "completed"}
    assert report["crud_probe"]["post_delete_found"] is False
    assert report["content_policy"] == {"memory_body_included": False}
    assert len(report["comparison_fingerprint"]) == 64
    assert "SENTINEL" not in serialized
    assert "representative baseline query" not in serialized
