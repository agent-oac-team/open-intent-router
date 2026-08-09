from app.core.config import Settings
from app.schemas.agent_context import (
    KnowledgeCitation,
    KnowledgeContextItem,
    MemoryContext,
    MemoryContextItem,
)
from app.schemas.agents import AgentDefinition
from app.schemas.context import ContextAssemblySession
from app.schemas.knowledge_provider import KnowledgeProviderResult
from app.schemas.memory import MemoryRecallResponse
from app.schemas.routing import RouteRequest
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.context_service import ContextService


def _agent(summarizer_agent, *, source_ids=None, scopes=None) -> AgentDefinition:
    payload = summarizer_agent.model_dump(mode="json")
    payload["context"] = {
        "memory": {
            "mode": "prefetch",
            "scopes": scopes or ["user_preference"],
            "max_items": 5,
        },
        "knowledge": {
            "mode": "prefetch",
            "source_ids": source_ids or ["docs"],
            "max_items": 5,
        },
    }
    return AgentDefinition.model_validate(payload)


def _request() -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "request_agent_context",
            "session_id": "session_agent_context",
            "user": {
                "id": "user_agent_context",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant_agent_context"},
            },
            "input": {"text": "risk rating"},
        }
    )


async def test_agent_projection_applies_declared_scope_source_status_and_total_budget(
    summarizer_agent,
) -> None:
    memory = StubMemoryService(status="ok")
    knowledge = StubKnowledgeService(status="ok")
    settings = Settings(
        context_pipeline_mode="enforced",
        context_agent_token_budget=300,
        context_per_item_char_limit=40,
    )
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=memory,
        knowledge_provider=knowledge,
    )
    invocation_input = {"text": "risk rating", "other": "x" * 40}

    request = _request()
    runtime = await service.assemble(
        agent=_agent(summarizer_agent, source_ids=["docs"], scopes=["stable_fact"]),
        user=request.user,
        session_id=request.session_id,
        query=request.input.text,
        invocation_input=invocation_input,
    )

    assert memory.requests[0].scopes == ["stable_fact"]
    assert knowledge.requests[0].source_ids == ["docs"]
    assert runtime.memory_context.status == "ok"
    assert runtime.knowledge_context.status == "ok"
    assert runtime.memory_context.truncated is True
    assert runtime.knowledge_context.truncated is True
    assert invocation_input["memory_context"]["status"] == "ok"
    assert invocation_input["knowledge_context"]["status"] == "ok"
    assert "context_trace" not in invocation_input
    assert runtime.metadata["context_pack"]["purpose"] == "agent_execution"
    assert runtime.metadata["input_token_estimate"] <= settings.context_agent_token_budget


async def test_agent_projection_keeps_stable_degraded_context_fields(summarizer_agent) -> None:
    for status in ("empty", "timeout", "error"):
        service = AgentContextAssemblyService(
            settings=Settings(context_pipeline_mode="enforced"),
            memory_service=StubMemoryService(status=status),
            knowledge_provider=StubKnowledgeService(status=status),
        )
        invocation_input = {"text": "risk rating"}

        runtime = await service.assemble(
            agent=_agent(summarizer_agent),
            user=_request().user,
            session_id=_request().session_id,
            query=_request().input.text,
            invocation_input=invocation_input,
        )

        assert runtime.memory_context.status == status
        assert runtime.knowledge_context.status == status
        assert invocation_input["memory_context"]["items"] == []
        assert invocation_input["knowledge_context"]["items"] == []


async def test_route_preview_defers_knowledge_retrieval_to_agent_invocation(
    summarizer_agent,
) -> None:
    knowledge = StubKnowledgeService(status="ok")
    settings = Settings(
        context_pipeline_mode="observe",
        context_route_knowledge_enabled=True,
        context_route_knowledge_source_ids="docs",
    )
    request = _request()
    assembly = ContextAssemblySession(
        assembly_id="assembly_agent_context",
        request_id=request.request_id,
        user_id=request.user.id,
        tenant_id=request.user.tenant_id,
    )
    route_context_service = ContextService(settings)
    await route_context_service.assemble_route_context(
        request,
        candidate_agent_ids=["summarizer"],
        candidate_agents=[],
        request_id=request.request_id,
        assembly_session=assembly,
    )
    assert knowledge.calls == 0
    agent_service = AgentContextAssemblyService(
        settings=settings,
        memory_service=StubMemoryService(status="empty"),
        knowledge_provider=knowledge,
    )
    invocation_input = {"text": "risk rating"}

    runtime = await agent_service.assemble_for_route(
        agent=_agent(summarizer_agent, source_ids=["docs"]),
        request=request,
        invocation_input=invocation_input,
        assembly_session=assembly,
    )

    assert knowledge.calls == 0
    assert runtime.knowledge_context.status == "disabled"
    assert "knowledge_context" not in invocation_input
    assert "context_pack" not in invocation_input
    assert "context_trace" not in invocation_input

    await agent_service.assemble_for_route(
        agent=_agent(summarizer_agent, source_ids=["docs"]),
        request=request,
        invocation_input={"text": "risk rating"},
        assembly_session=ContextAssemblySession(
            assembly_id="assembly_second",
            request_id=request.request_id,
            user_id=request.user.id,
            tenant_id=request.user.tenant_id,
        ),
    )
    assert knowledge.calls == 0


class StubMemoryService:
    def __init__(self, *, status: str) -> None:
        self.status = status
        self.requests = []

    async def recall(self, request):
        self.requests.append(request)
        items = []
        if self.status == "ok":
            items = [
                MemoryContextItem(
                    memory_id="memory_1",
                    scope="stable_fact",
                    content="prefers concise answers " * 10,
                    relevance=0.9,
                )
            ]
        return MemoryRecallResponse(
            context=MemoryContext(
                summary="memory summary" if items else "",
                items=items,
                status=self.status,
                errors=[f"memory_{self.status}"] if self.status in {"timeout", "error"} else [],
            )
        )


class StubKnowledgeService:
    def __init__(self, *, status: str) -> None:
        self.status = status
        self.requests = []
        self.calls = 0

    async def retrieve(self, request):
        self.calls += 1
        self.requests.append(request)
        items = []
        citations = []
        if self.status == "ok":
            citation = KnowledgeCitation(source_id="docs", chunk_id="chunk_1")
            items = [
                KnowledgeContextItem(
                    item_id="chunk_1",
                    source_id="docs",
                    content="risk rating guide " * 10,
                    score=0.9,
                    citation=citation,
                )
            ]
            citations = [citation]
        return KnowledgeProviderResult(
            status=self.status,
            items=items,
            citations=citations,
            error_code=(
                f"knowledge_{self.status}" if self.status in {"timeout", "error"} else None
            ),
        )
