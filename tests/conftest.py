import os

import pytest

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
from app.schemas.agents import AgentDefinition
from app.services.registry_service import AgentRegistryService

TEST_ENV_DEFAULTS = {
    "APP_ENV": "local",
    "DATABASE_URL": "sqlite+aiosqlite:///./data/test-open-intent-router.db",
    "STORAGE_BACKEND": "memory",
    "ROUTER_LLM_PROVIDER": "mock",
    "ROUTER_LLM_MODEL": "mock-router",
    "ROUTER_LLM_BASE_URL": "",
    "ROUTER_LLM_API_KEY": "",
    "MEMORY_STRATEGY_PROVIDER": "memory",
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
def summarizer_agent() -> AgentDefinition:
    return AgentDefinition.model_validate(
        {
            "agent_id": "summarizer",
            "name": "Summarizer",
            "description": "Summarize text",
            "type": "mock",
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
            "invocation": {"type": "mock", "config": {"response": {"summary": "ok"}}},
        }
    )


@pytest.fixture
def task_creator_agent() -> AgentDefinition:
    return AgentDefinition.model_validate(
        {
            "agent_id": "task_creator",
            "name": "Task Creator",
            "description": "Create a task or to-do item from user instructions.",
            "type": "mock",
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
            "invocation": {
                "type": "mock",
                "config": {"response": {"task_id": "task_mock_001", "title": "Mock task"}},
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
    service = AgentRegistryService(settings=settings, repository=repositories["registry"])
    await service.load()
    return service
