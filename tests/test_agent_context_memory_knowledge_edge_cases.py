from app.core.config import Settings
from app.repositories.context_stores import KnowledgeRepository, MemoryItemRepository
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.schemas.agents import AgentDefinition
from app.schemas.common import UserContext
from app.schemas.knowledge import KnowledgeChunk, KnowledgeSearchRequest, KnowledgeSource
from app.schemas.memory import MemoryItem
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.knowledge_service import KnowledgeService
from app.services.memory_service import MemoryService
from app.services.registry_service import AgentRegistryService


async def test_prefetch_max_items_zero_does_not_fallback_to_default(summarizer_agent) -> None:
    settings = Settings(storage_backend="memory")
    memory_repository = MemoryItemRepository()
    knowledge_repository = KnowledgeRepository()
    await memory_repository.add(
        MemoryItem(
            scope="user_preference",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="prefers concise risk summaries",
        )
    )
    await knowledge_repository.upsert_source(
        KnowledgeSource(source_id="docs", name="Docs", allow_tenants=["t1"])
    )
    await knowledge_repository.add_chunk(
        KnowledgeChunk(source_id="docs", content="risk rating guide")
    )
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(settings=settings, repository=memory_repository),
        knowledge_service=KnowledgeService(settings=settings, repository=knowledge_repository),
    )
    agent = _context_agent(
        summarizer_agent,
        memory={"mode": "prefetch", "scopes": ["user_preference"], "max_items": 0},
        knowledge={"mode": "prefetch", "source_ids": ["docs"], "max_items": 0},
    )
    invocation_input = {"text": "risk rating"}

    runtime = await service.assemble(
        agent=agent,
        user=UserContext(id="u1", roles=["operator"], attributes={"tenant_id": "t1"}),
        session_id="s1",
        query="risk rating",
        invocation_input=invocation_input,
    )

    assert runtime.memory_context.items == []
    assert runtime.knowledge_context.items == []
    assert invocation_input["memory_context"]["items"] == []
    assert invocation_input["knowledge_context"]["items"] == []


async def test_controlled_retrieval_max_items_zero_does_not_search(summarizer_agent) -> None:
    settings = Settings(storage_backend="memory")
    repository = KnowledgeRepository()
    await repository.upsert_source(KnowledgeSource(source_id="docs", name="Docs"))
    await repository.add_chunk(KnowledgeChunk(source_id="docs", content="risk rating guide"))
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(settings=settings, repository=MemoryItemRepository()),
        knowledge_service=KnowledgeService(settings=settings, repository=repository),
    )
    agent = _context_agent(
        summarizer_agent,
        knowledge={
            "mode": "controlled_retrieval",
            "source_ids": ["docs"],
            "max_items": 0,
            "controlled_retrieval": {
                "query_template": "{{ input.text }}",
                "allowed_variables": ["input.text"],
            },
        },
    )

    context = await service.controlled_knowledge_retrieval(
        agent=agent,
        user=UserContext(id="u1"),
        variables={"input": {"text": "risk rating"}},
    )

    assert context.items == []


async def test_direct_invoke_with_partial_existing_context_assembles_missing_context(
    summarizer_agent,
) -> None:
    settings = Settings(storage_backend="memory", registry_backend="database")
    agent = _context_agent(
        summarizer_agent,
        memory={"mode": "prefetch", "scopes": ["user_preference"], "max_items": 3},
        knowledge={"mode": "prefetch", "source_ids": ["docs"], "max_items": 3},
    )
    registry_repository = MemoryAgentDefinitionRepository()
    await registry_repository.upsert(agent)
    registry = AgentRegistryService(settings=settings, repository=registry_repository)
    await registry.load()
    knowledge_repository = KnowledgeRepository()
    await knowledge_repository.upsert_source(KnowledgeSource(source_id="docs", name="Docs"))
    await knowledge_repository.add_chunk(
        KnowledgeChunk(source_id="docs", content="risk rating guide")
    )
    context_service = AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(settings=settings, repository=MemoryItemRepository()),
        knowledge_service=KnowledgeService(settings=settings, repository=knowledge_repository),
    )
    run_repository = MemoryRunRepository()
    invocation_service = InvocationService(
        registry=registry,
        run_repository=run_repository,
        result_repository=MemoryResultRepository(),
        invokers=build_default_invoker_registry(settings),
        agent_context_service=context_service,
    )

    result = await invocation_service.invoke_agent(
        agent_id=agent.agent_id,
        session_id="s1",
        user=UserContext(id="u1"),
        input={"text": "risk rating", "memory_context": {"status": "ok", "items": []}},
    )

    run = await run_repository.get_run(result.run_id)
    assert run is not None
    assert run.input["memory_context"]["status"] == "ok"
    assert run.input["knowledge_context"]["items"][0]["source_id"] == "docs"


async def test_caller_type_admin_does_not_bypass_source_policy() -> None:
    repository = KnowledgeRepository()
    await repository.upsert_source(
        KnowledgeSource(source_id="secret", name="Secret", allow_roles=["admin"])
    )
    await repository.add_chunk(KnowledgeChunk(source_id="secret", content="risk secret"))
    service = KnowledgeService(settings=Settings(storage_backend="memory"), repository=repository)

    response = await service.search(
        KnowledgeSearchRequest(
            query="risk",
            user=UserContext(id="u1", roles=["operator"]),
            caller_type="admin",
            purpose="debug",
            source_ids=["secret"],
        )
    )

    assert response.context.items == []
    assert response.denied_source_ids == ["secret"]


def test_knowledge_search_api_rejects_invalid_boundary_values() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())

    invalid_top_k = client.post(
        "/api/v1/knowledge/search",
        json={
            "query": "risk",
            "user": {"id": "u1"},
            "source_ids": ["docs"],
            "top_k": 51,
        },
    )
    invalid_caller = client.post(
        "/api/v1/knowledge/search",
        json={
            "query": "risk",
            "user": {"id": "u1"},
            "caller_type": "root",
        },
    )

    assert invalid_top_k.status_code == 422
    assert invalid_caller.status_code == 422


def test_memory_api_rejects_invalid_boundary_values() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())

    invalid_max_items = client.post(
        "/api/v1/memories/recall",
        json={"query": "risk", "user": {"id": "u1"}, "max_items": 51},
    )
    missing_user_id = client.post(
        "/api/v1/memories/write-candidates",
        json=[{"scope": "stable_fact", "content": "confirmed fact"}],
    )

    assert invalid_max_items.status_code == 422
    assert missing_user_id.status_code == 422


def _context_agent(
    agent: AgentDefinition,
    *,
    memory: dict | None = None,
    knowledge: dict | None = None,
) -> AgentDefinition:
    payload = agent.model_dump(mode="json")
    payload["context"] = {
        "memory": memory or {"mode": "disabled"},
        "knowledge": knowledge or {"mode": "disabled"},
    }
    return AgentDefinition.model_validate(payload)
