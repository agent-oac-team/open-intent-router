from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.memory_runtime import build_memory_runtime_policy
from app.dependencies import get_registry_service
from app.main import create_app
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.schemas.agents import AgentDefinition
from app.services.registry_service import AgentRegistryService


def _runtime_config_response(app):
    with TestClient(app) as client:
        return client.get("/api/v1/runtime/config")


def test_runtime_config_exposes_safe_status() -> None:
    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        router_llm_provider="openai_compatible",
        router_llm_model="deepseek-chat",
        router_llm_base_url="https://api.deepseek.com",
        router_llm_api_key="secret-key",
        admin_api_token="admin-secret",
        context_pipeline_mode="observe",
        context_policy_version="policy-test",
        context_budget_version="budget-test",
        context_projection_version="projection-test",
        memory_mode="observe",
        memory_formation_model="private-provider-model-name",
        memory_formation_model_version="formation-model-test",
        memory_formation_prompt_version="formation-prompt-test",
        memory_formation_policy_version="formation-policy-test",
    )
    repository = MemoryAgentDefinitionRepository()
    app = create_app(settings=settings)
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=repository,
    )

    response = _runtime_config_response(app)

    assert response.status_code == 200
    body = response.json()
    assert body["router_llm_provider"] == "openai_compatible"
    assert body["router_llm_model"] == "deepseek-chat"
    assert body["router_llm_base_url"] == "https://api.deepseek.com"
    assert body["router_llm_api_key_configured"] is True
    assert body["admin_api_token_configured"] is True
    assert body["admin_auth_mode"] == "token_required"
    assert body["registry_mutation_mode"] == "token_required"
    assert body["memory_mode"] == "observe"
    assert body["memory_enabled"] is False
    assert body["memory_recall_enabled"] is False
    assert body["memory_strategy_provider"] == "memory"
    assert body["memory_formation_mode"] == "observe"
    assert body["memory_execution_mode"] == "live"
    assert body["memory_turn_outbox_consumer_enabled"] is True
    assert body["memory_formation_model_version"] == "formation-model-test"
    assert body["memory_formation_prompt_version"] == "formation-prompt-test"
    assert body["memory_formation_policy_version"] == "formation-policy-test"
    assert body["memory_formation_worker_enabled"] is True
    assert body["memory_formation_sweeper_enabled"] is True
    assert body["memory_formation_queue_depth"] == 0
    assert body["context_pipeline_mode"] == "observe"
    assert body["context_route_memory_enabled"] is False
    assert body["context_policy_version"] == "policy-test"
    assert body["context_budget_version"] == "budget-test"
    assert body["context_projection_version"] == "projection-test"
    serialized = str(body)
    assert "secret-key" not in serialized
    assert "admin-secret" not in serialized
    assert "private-provider-model-name" not in serialized


async def test_runtime_config_reports_registry_agent_count(settings, summarizer_agent) -> None:
    repository = MemoryAgentDefinitionRepository()
    await repository.upsert(AgentDefinition.model_validate(summarizer_agent.model_dump()))
    registry = AgentRegistryService(settings=settings, repository=repository)
    await registry.load()
    app = create_app(settings=settings)
    app.dependency_overrides[get_registry_service] = lambda: registry

    response = _runtime_config_response(app)

    assert response.status_code == 200
    body = response.json()
    assert body["registry_status"] == "ok"
    assert body["registry_active_source"] == "database"
    assert body["registry_agent_count"] == 1
    assert body["admin_auth_mode"] == "token_required"
    assert body["registry_mutation_mode"] == "token_required"


def test_runtime_config_reports_effective_decision_shadow_worker_states() -> None:
    settings = Settings(storage_backend="memory", memory_mode="on")
    app = create_app(
        settings=settings,
        memory_runtime_policy=build_memory_runtime_policy("on", execution_plane="decision_shadow"),
    )
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=MemoryAgentDefinitionRepository(),
    )

    response = _runtime_config_response(app)

    assert response.status_code == 200
    body = response.json()
    assert body["memory_execution_mode"] == "decision_shadow"
    assert body["memory_formation_worker_enabled"] is False
    assert body["memory_formation_sweeper_enabled"] is False
    assert body["memory_index_worker_enabled"] is False
    assert body["memory_ttl_sweeper_enabled"] is False


def test_runtime_config_reports_local_dev_write_mode() -> None:
    settings = Settings(
        app_env="local",
        admin_api_token=None,
        storage_backend="memory",
        registry_backend="database",
    )
    repository = MemoryAgentDefinitionRepository()
    app = create_app(settings=settings)
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=repository,
    )

    response = _runtime_config_response(app)

    assert response.status_code == 200
    body = response.json()
    assert body["admin_api_token_configured"] is False
    assert body["admin_auth_mode"] == "local_loopback_open"
    assert body["registry_mutation_mode"] == "local_dev_write_enabled"


def test_runtime_config_redacts_connection_credentials() -> None:
    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        router_llm_base_url="https://router-user:router-pass@example.test/v1?token=secret",
        memory_milvus_uri="https://milvus-user:milvus-pass@milvus.test:19530/db?token=secret",
    )
    repository = MemoryAgentDefinitionRepository()
    app = create_app(settings=settings)
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=repository,
    )
    response = _runtime_config_response(app)
    assert response.status_code == 200
    body = response.json()
    assert body["router_llm_base_url"] == "https://example.test/v1"
    assert body["memory_mem0_milvus_uri"] == "https://milvus.test:19530/db"
    serialized = str(body)
    assert "router-pass" not in serialized
    assert "milvus-pass" not in serialized


def test_runtime_config_exposes_effective_memory_infrastructure_sources() -> None:
    settings = Settings(
        _env_file=None,
        storage_backend="memory",
        registry_backend="database",
        memory_database_url="postgresql+asyncpg://memory-user:memory-pass@db.test:5432/oir",
        memory_milvus_uri="https://milvus-user:milvus-pass@milvus.test:19530/memory",
        memory_milvus_collection="existing-memory-vectors",
        memory_embedding_base_url="https://embed-user:embed-pass@embed.test/v1",
        memory_embedding_api_key="embedding-secret",
        memory_embedding_model="memory-embedding-v2",
        memory_embedding_dims=1536,
    )
    app = create_app(settings=settings)
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=MemoryAgentDefinitionRepository(),
    )

    response = _runtime_config_response(app)

    assert response.status_code == 200
    body = response.json()
    assert body["memory_database_url"] == "postgresql+asyncpg://db.test:5432/oir"
    assert body["memory_mem0_milvus_uri"] == "https://milvus.test:19530/memory"
    assert body["memory_mem0_collection"] == "existing-memory-vectors"
    assert body["memory_embedding_model"] == "memory-embedding-v2"
    assert body["memory_embedding_dims"] == 1536
    assert body["memory_infrastructure_sources"]["database_url"] == "MEMORY_DATABASE_URL"
    assert body["memory_infrastructure_sources"]["milvus_uri"] == "MEMORY_MILVUS_URI"
    assert "memory_deprecated_config_fallbacks" not in body
    assert "memory-pass" not in str(body)
    assert "milvus-pass" not in str(body)
    assert "embedding-secret" not in str(body)
