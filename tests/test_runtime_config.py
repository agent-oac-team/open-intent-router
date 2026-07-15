from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.dependencies import get_registry_service
from app.main import create_app
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.schemas.agents import AgentDefinition
from app.services.registry_service import AgentRegistryService


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
        context_route_memory_enabled=True,
        context_route_knowledge_enabled=True,
        context_policy_version="policy-test",
        context_budget_version="budget-test",
        context_projection_version="projection-test",
        memory_formation_mode="observe",
        memory_formation_model="private-provider-model-name",
        memory_formation_model_version="formation-model-test",
        memory_formation_prompt_version="formation-prompt-test",
        memory_formation_policy_version="formation-policy-test",
        memory_formation_worker_enabled=True,
        memory_formation_sweeper_enabled=True,
    )
    repository = MemoryAgentDefinitionRepository()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=repository,
    )

    client = TestClient(app)
    response = client.get("/api/v1/runtime/config")

    assert response.status_code == 200
    body = response.json()
    assert body["router_llm_provider"] == "openai_compatible"
    assert body["router_llm_model"] == "deepseek-chat"
    assert body["router_llm_base_url"] == "https://api.deepseek.com"
    assert body["router_llm_api_key_configured"] is True
    assert body["admin_api_token_configured"] is True
    assert body["admin_auth_mode"] == "token_required"
    assert body["registry_mutation_mode"] == "token_required"
    assert body["memory_enabled"] is True
    assert body["memory_strategy_provider"] == "memory"
    assert body["memory_formation_mode"] == "observe"
    assert body["memory_formation_model_version"] == "formation-model-test"
    assert body["memory_formation_prompt_version"] == "formation-prompt-test"
    assert body["memory_formation_policy_version"] == "formation-policy-test"
    assert body["memory_formation_worker_enabled"] is True
    assert body["memory_formation_sweeper_enabled"] is True
    assert body["memory_formation_queue_depth"] == 0
    assert body["knowledge_enabled"] is True
    assert body["knowledge_vector_backend"] == "memory"
    assert body["context_pipeline_mode"] == "observe"
    assert body["context_route_memory_enabled"] is True
    assert body["context_route_knowledge_enabled"] is True
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
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_registry_service] = lambda: registry

    client = TestClient(app)
    response = client.get("/api/v1/runtime/config")

    assert response.status_code == 200
    body = response.json()
    assert body["registry_status"] == "ok"
    assert body["registry_active_source"] == "database"
    assert body["registry_agent_count"] == 1
    assert body["admin_auth_mode"] == "token_required"
    assert body["registry_mutation_mode"] == "token_required"


def test_runtime_config_reports_local_dev_write_mode() -> None:
    settings = Settings(
        app_env="local",
        admin_api_token=None,
        storage_backend="memory",
        registry_backend="database",
    )
    repository = MemoryAgentDefinitionRepository()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=repository,
    )

    client = TestClient(app)
    response = client.get("/api/v1/runtime/config")

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
        memory_mem0_milvus_uri="https://milvus-user:milvus-pass@milvus.test:19530/db?token=secret",
        knowledge_milvus_uri="https://knowledge-user:knowledge-pass@knowledge.test/vector",
    )
    repository = MemoryAgentDefinitionRepository()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_registry_service] = lambda: AgentRegistryService(
        settings=settings,
        repository=repository,
    )
    response = TestClient(app).get("/api/v1/runtime/config")
    assert response.status_code == 200
    body = response.json()
    assert body["router_llm_base_url"] == "https://example.test/v1"
    assert body["memory_mem0_milvus_uri"] == "https://milvus.test:19530/db"
    assert body["knowledge_milvus_uri"] == "https://knowledge.test/vector"
    serialized = str(body)
    assert "router-pass" not in serialized
    assert "milvus-pass" not in serialized
    assert "knowledge-pass" not in serialized
