import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
    MemoryEventRepository,
    MemoryMessageRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRouteLogRepository,
    MemoryRunRepository,
)
from app.schemas.agents import AgentDefinitionV2
from tests.support.database import ManagedTestDatabases
from tests.support.v2_runtime import SnapshotAwareRegistryService, activate_v2_test_catalog

TEST_ENV_DEFAULTS = {
    "APP_ENV": "local",
    "DATABASE_URL": "sqlite+aiosqlite:///./data/test-open-intent-router.db",
    "STORAGE_BACKEND": "memory",
    "ROUTER_LLM_PROVIDER": "mock",
    "ROUTER_LLM_API_STYLE": "chat_completions",
    "ROUTER_LLM_MODEL": "mock-router",
    "ROUTER_LLM_BASE_URL": "",
    "ROUTER_LLM_API_KEY": "",
    "MEMORY_STRATEGY_PROVIDER": "memory",
    "MEMORY_MODE": "on",
    "MEMORY_DATABASE_URL": "sqlite+aiosqlite:///./data/test-open-intent-router.db",
    "MEMORY_MILVUS_COLLECTION": "oir_memory_vectors",
    "MEMORY_MILVUS_URI": ".data/oir_memory_milvus.db",
    "MEMORY_EMBEDDING_MODEL": "text-embedding-v4",
    "MEMORY_EMBEDDING_DIMS": "1024",
    "MEMORY_MEM0_LLM_MODEL": "test-memory-model",
    "MEMORY_MEM0_LLM_BASE_URL": "https://memory-llm.test/v1",
    "MEMORY_MEM0_LLM_API_KEY": "test-memory-key",
    "OAC_HOST_IDENTITY_CURRENT_KEY_ID": "test-user-key",
    "OAC_HOST_IDENTITY_CURRENT_KEY": "test-user-secret",
    "OAC_HOST_OAC_ADMIN_KEY_ID": "test-admin-key",
    "OAC_HOST_OAC_ADMIN_CREDENTIAL": "test-admin-secret",
    "OAC_HOST_COZE_WORKFLOW_KEY_ID": "test-coze-key",
    "OAC_HOST_COZE_WORKFLOW_CREDENTIAL": "test-coze-secret",
}

# Tests should not inherit the developer's local .env. Individual tests can still
# pass explicit Settings fields or use monkeypatch for env-specific behavior.
Settings.model_config["env_file"] = None

for key, value in TEST_ENV_DEFAULTS.items():
    os.environ[key] = value
os.environ.pop("MEMORY_MEM0_FAIL_CLOSED", None)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        storage_backend="memory",
        registry_backend="database",
        router_llm_provider="mock",
        admin_api_token="test-token",
    )


@pytest.fixture
async def managed_database() -> ManagedTestDatabases:
    """Provide explicit fixture-owned database scopes to a test."""

    databases = ManagedTestDatabases()
    try:
        yield databases
    finally:
        await databases.aclose()


@pytest.fixture
def non_lifespan_test_client():
    """Create clients whose transport is closed without starting app lifespan."""

    clients: list[TestClient] = []

    def create(*args, **kwargs) -> TestClient:
        client = TestClient(*args, **kwargs)
        clients.append(client)
        return client

    try:
        yield create
    finally:
        for client in reversed(clients):
            client.close()


@pytest.fixture
def summarizer_agent() -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "summarizer",
            "name": "Summarizer",
            "description": "Summarize text",
            "capabilities": ["summarize"],
            "trigger": {"keywords": ["summarize"]},
            "access_policy": {"allow_roles": ["operator"], "allow_tenants": ["*"]},
            "required_inputs": ["text"],
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            },
            "handling": {
                "kind": "invocation",
                "adapter_key": "mock",
                "config": {"function": "summarize"},
            },
        }
    )


@pytest.fixture
def task_creator_agent() -> AgentDefinitionV2:
    return AgentDefinitionV2.model_validate(
        {
            "schema_version": "oir-agent-v2",
            "agent_id": "task_creator",
            "name": "Task Creator",
            "description": "Create a task or to-do item from user instructions.",
            "capabilities": ["create_task"],
            "trigger": {
                "keywords": ["task", "todo"],
                "positive_examples": ["create a task from this summary"],
            },
            "access_policy": {"allow_roles": ["operator"], "allow_tenants": ["*"]},
            "required_inputs": ["title"],
            "input_schema": {
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}, "title": {"type": "string"}},
            },
            "handling": {
                "kind": "invocation",
                "adapter_key": "mock",
                "config": {"function": "create_task"},
            },
        }
    )


@pytest.fixture
def repositories():
    return {
        "registry": MemoryAgentDefinitionRepository(),
        "messages": MemoryMessageRepository(),
        "events": MemoryEventRepository(),
        "runs": MemoryRunRepository(),
        "results": MemoryResultRepository(),
        "plans": MemoryPlanRepository(),
        "route_logs": MemoryRouteLogRepository(),
    }


@pytest.fixture
async def registry_service(settings, repositories, summarizer_agent):
    await repositories["registry"].upsert(summarizer_agent)
    catalog = await activate_v2_test_catalog()
    service = SnapshotAwareRegistryService(
        settings=settings,
        repository=repositories["registry"],
        runtime_catalog=catalog,
    )
    try:
        await service.load()
        yield service
    finally:
        await catalog.aclose()
