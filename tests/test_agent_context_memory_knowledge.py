import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory import (
    MemoryAgentDefinitionRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.schemas.agent_context import (
    KnowledgeCitation,
    KnowledgeContextItem,
)
from app.schemas.agents import AgentDefinition
from app.schemas.common import UserContext
from app.schemas.knowledge_provider import KnowledgeProviderResult
from app.schemas.memory import MemoryItem, MemoryRecallRequest, MemoryWriteCandidate
from app.schemas.routing import RouteRequest
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.memory_service import MemoryService
from app.services.registry_service import AgentRegistryService
from app.services.router_service import RouterService


def test_agent_context_schema_accepts_valid_prefetch_modes(summarizer_agent) -> None:
    payload = summarizer_agent.model_dump(mode="json")
    payload["context"] = {
        "memory": {
            "mode": "prefetch",
            "scopes": ["user_preference", "stable_fact", "task_memory"],
            "max_items": 3,
        },
        "knowledge": {
            "mode": "prefetch",
            "source_ids": ["product_docs"],
            "source_tags": ["public"],
            "max_items": 4,
        },
    }

    agent = AgentDefinition.model_validate(payload)

    assert agent.context.memory.mode == "prefetch"
    assert agent.context.memory.scopes == ["user_preference", "stable_fact", "task_memory"]
    assert agent.context.knowledge.mode == "prefetch"
    assert agent.context.knowledge.source_ids == ["product_docs"]


def test_agent_context_schema_rejects_invalid_scope_and_missing_template(summarizer_agent) -> None:
    invalid_scope = summarizer_agent.model_dump(mode="json")
    invalid_scope["context"] = {"memory": {"mode": "prefetch", "scopes": ["private_guess"]}}

    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(invalid_scope)

    missing_template = summarizer_agent.model_dump(mode="json")
    missing_template["context"] = {"knowledge": {"mode": "controlled_retrieval"}}

    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(missing_template)


async def test_memory_recall_empty_success_scope_ttl_conflict_and_cleanup() -> None:
    settings = Settings(storage_backend="memory", registry_backend="database")
    repository = MemoryItemRepository()
    service = MemoryService(settings=settings, repository=repository)
    user = UserContext(id="u1", roles=["operator"], attributes={"tenant_id": "t1"})

    empty = await service.recall(MemoryRecallRequest(query="hello", user=user))
    assert empty.context.status == "empty"

    decisions = await service.write_candidates(
        candidates=[
            MemoryWriteCandidate(
                scope="task_memory",
                content="prepare account summary",
                confidence=0.9,
                source="plan_result",
            ),
            MemoryWriteCandidate(
                scope="user_preference",
                content="用户偏好中文输出",
                confidence=0.9,
                source="message",
            ),
        ],
        user_id="u1",
        tenant_id="t1",
    )
    assert [decision.status for decision in decisions] == ["accepted", "accepted"]

    task_item = repository.items[decisions[0].memory_id]
    assert task_item.ttl_expires_at is not None
    assert task_item.ttl_expires_at > datetime.now(UTC) + timedelta(days=13)

    task_recall = await service.recall(
        MemoryRecallRequest(query="summary", user=user, scopes=["task_memory"], max_items=5)
    )
    assert task_recall.context.status == "ok"
    assert [item.scope for item in task_recall.context.items] == ["task_memory"]

    conflict = await service.recall(
        MemoryRecallRequest(query="这次请用英文输出", user=user, scopes=["user_preference"])
    )
    assert conflict.context.metadata["conflicts"][0]["reason"] == "current_input_override"

    expired = MemoryItem(
        scope="task_memory",
        subject_id="u1",
        user_id="u1",
        tenant_id="t1",
        content="expired task output",
        ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await repository.add(expired)
    cleanup = await service.cleanup_expired()
    assert expired.memory_id in cleanup.expired_memory_ids
    assert cleanup.expired_count == 1
    assert any(event.event_type == "memory_deletion_requested" for event in repository.events)
    assert repository.items[expired.memory_id].lifecycle_status == "deletion_pending"


async def test_memory_write_policy_rejection_and_debug_visibility() -> None:
    service = MemoryService(
        settings=Settings(storage_backend="memory"), repository=MemoryItemRepository()
    )

    decisions = await service.write_candidates(
        candidates=[
            MemoryWriteCandidate(scope="stable_fact", content="", confidence=0.9),
            MemoryWriteCandidate(
                scope="stable_fact", content="sensitive", metadata={"sensitive": True}
            ),
            MemoryWriteCandidate(scope="stable_fact", content="low", confidence=0.2),
            MemoryWriteCandidate(scope="stable_fact", content="confirmed fact", confidence=0.9),
        ],
        user_id="u1",
        tenant_id="t1",
    )

    assert [decision.status for decision in decisions] == [
        "rejected",
        "rejected",
        "rejected",
        "accepted",
    ]
    debug = await service.debug_state(user_id="u1", tenant_id="t1")
    assert debug.metadata["item_count"] == 1
    assert debug.metadata["event_count"] == 1
    assert debug.items[0].content == "confirmed fact"


async def test_memory_timeout_degrades_in_agent_context(summarizer_agent) -> None:
    settings = Settings(storage_backend="memory", memory_prefetch_timeout_seconds=0.01)
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=SlowMemoryService(settings=settings),
        knowledge_provider=StaticKnowledgeProvider({}),
    )
    payload = summarizer_agent.model_dump(mode="json")
    payload["context"] = {"memory": {"mode": "prefetch", "scopes": ["user_preference"]}}
    agent = AgentDefinition.model_validate(payload)
    invocation_input = {"text": "hello"}

    runtime = await service.assemble(
        agent=agent,
        user=UserContext(id="u1"),
        session_id="s1",
        query="hello",
        invocation_input=invocation_input,
    )

    assert runtime.memory_context.status == "timeout"
    assert invocation_input["memory_context"]["status"] == "timeout"


async def test_router_preview_defers_knowledge_body_until_invocation(summarizer_agent) -> None:
    settings = Settings(storage_backend="memory", registry_backend="database")
    agent = await _context_agent(settings, summarizer_agent)
    registry = await _registry(settings, agent)
    memory_repository = MemoryItemRepository()
    await memory_repository.add(
        MemoryItem(
            scope="user_preference",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="prefers concise answers",
        )
    )
    context_service = AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(settings=settings, repository=memory_repository),
        knowledge_provider=StaticKnowledgeProvider({"docs": "risk rating basics for customers"}),
    )
    route_service = RouterService(
        settings=settings,
        registry=registry,
        llm_client=FixedTargetLLM(agent.agent_id),
        agent_context_service=context_service,
    )

    route_response = await route_service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "risk rating"},
            }
        )
    )

    assert route_response.invocation
    assert route_response.invocation.input["memory_context"]["summary"]
    assert "knowledge_context" not in route_response.invocation.input
    assert route_response.context.metadata["agent_context"]["memory_item_count"] == 1
    assert route_response.context.metadata["agent_context"]["knowledge_item_count"] == 0

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
        user=UserContext(id="u1", roles=["operator"], attributes={"tenant_id": "t1"}),
        input={"text": "risk rating"},
    )

    assert result.status == "completed"
    run = await run_repository.get_run(result.run_id)
    assert run.input["memory_context"]["items"][0]["content"] == "prefers concise answers"
    assert run.input["knowledge_context"]["citations"][0]["source_id"] == "docs"
    knowledge_item_ref = run.input["knowledge_context"]["items"][0]
    assert knowledge_item_ref["item_id"]
    assert knowledge_item_ref["source_id"] == "docs"
    assert set(knowledge_item_ref) == {"item_id", "source_id"}
    assert "summary" not in run.input["knowledge_context"]


async def test_controlled_retrieval_template_allowed_variables_and_denied_sources(
    summarizer_agent,
) -> None:
    settings = Settings(storage_backend="memory")
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(settings=settings, repository=MemoryItemRepository()),
        knowledge_provider=StaticKnowledgeProvider(
            {"docs": "risk rating public guide"},
            denied_source_ids={"secret"},
        ),
    )
    payload = summarizer_agent.model_dump(mode="json")
    payload["context"] = {
        "knowledge": {
            "mode": "controlled_retrieval",
            "source_ids": ["docs", "secret"],
            "max_items": 5,
            "controlled_retrieval": {
                "query_template": "{{ input.text }} {{ secret }}",
                "allowed_variables": ["input.text"],
                "output_key": "knowledge_context",
            },
        }
    }
    agent = AgentDefinition.model_validate(payload)

    context = await service.controlled_knowledge_retrieval(
        agent=agent,
        user=UserContext(id="u1", roles=["operator"]),
        variables={"input": {"text": "risk rating"}, "secret": "token"},
    )

    assert context.status == "ok"
    assert context.items[0].source_id == "docs"
    assert context.metadata["denied_source_ids"] == ["secret"]
    assert "token" not in context.summary


async def test_context_pack_budget_truncates_memory_and_knowledge_items(summarizer_agent) -> None:
    settings = Settings(storage_backend="memory", context_per_item_char_limit=8)
    agent = await _context_agent(settings, summarizer_agent)
    memory_repository = MemoryItemRepository()
    await memory_repository.add(
        MemoryItem(
            scope="user_preference",
            subject_id="u1",
            user_id="u1",
            content="abcdefghijklmnopqrstuvwxyz",
        )
    )
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=MemoryService(settings=settings, repository=memory_repository),
        knowledge_provider=StaticKnowledgeProvider({"docs": "abcdefghijklmnopqrstuvwxyz"}),
    )
    invocation_input = {"text": "abcdefghijklmnopqrstuvwxyz"}

    runtime = await service.assemble(
        agent=agent,
        user=UserContext(id="u1"),
        session_id="s1",
        query="abcdefghijklmnopqrstuvwxyz",
        invocation_input=invocation_input,
    )

    assert runtime.memory_context.truncated is True
    assert runtime.knowledge_context.truncated is True
    assert runtime.memory_context.items[0].content == "abcdefgh"
    assert runtime.knowledge_context.items[0].content == "abcdefgh"


async def _registry(settings: Settings, agent: AgentDefinition) -> AgentRegistryService:
    repository = MemoryAgentDefinitionRepository()
    await repository.upsert(agent)
    registry = AgentRegistryService(settings=settings, repository=repository)
    await registry.load()
    return registry


async def _context_agent(settings: Settings, summarizer_agent) -> AgentDefinition:
    payload = summarizer_agent.model_dump(mode="json")
    payload["context"] = {
        "memory": {
            "mode": "prefetch",
            "scopes": ["user_preference"],
            "max_items": settings.memory_default_max_items,
        },
        "knowledge": {
            "mode": "prefetch",
            "source_ids": ["docs"],
            "max_items": settings.knowledge_default_max_items,
        },
    }
    return AgentDefinition.model_validate(payload)


class FixedTargetLLM:
    def __init__(self, target_agent_id: str) -> None:
        self.target_agent_id = target_agent_id

    async def route(self, payload):
        from app.schemas.routing import RouteContext, RouteDecision, RouteResponse

        return RouteResponse(
            request_id=payload.request.request_id or "req_context",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id=self.target_agent_id,
                confidence=0.9,
                reason="context test",
                message="context test",
            ),
            context=RouteContext(
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates]
            ),
        )


class SlowMemoryService(MemoryService):
    async def recall(self, request: MemoryRecallRequest):
        await asyncio.sleep(0.05)
        return await super().recall(request)


class StaticKnowledgeProvider:
    def __init__(
        self,
        content_by_source: dict[str, str],
        *,
        denied_source_ids: set[str] | None = None,
    ) -> None:
        self.content_by_source = content_by_source
        self.denied_source_ids = denied_source_ids or set()

    async def retrieve(self, request):
        items = []
        for source_id in request.source_ids:
            content = self.content_by_source.get(source_id)
            if content is None or source_id in self.denied_source_ids:
                continue
            item_id = f"{source_id}-item"
            citation = KnowledgeCitation(source_id=source_id)
            items.append(
                KnowledgeContextItem(
                    item_id=item_id,
                    source_id=source_id,
                    content=content,
                    score=0.9,
                    citation=citation,
                )
            )
        denied = [
            source_id for source_id in request.source_ids if source_id in self.denied_source_ids
        ]
        return KnowledgeProviderResult(
            status="ok" if items else "empty",
            items=items[: request.budget.max_items],
            citations=[item.citation for item in items if item.citation is not None],
            metadata={"denied_source_ids": denied},
        )
