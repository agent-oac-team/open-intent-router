from app.core.config import Settings
from app.plugins.knowledge import KnowledgeProvider
from app.repositories.context_stores import MemoryItemRepository
from app.schemas.agent_context import KnowledgeCitation, KnowledgeContextItem
from app.schemas.agents import AgentDefinitionV2
from app.schemas.common import UserContext
from app.schemas.knowledge_provider import (
    KnowledgeProviderRequest,
    KnowledgeProviderResult,
)
from app.schemas.routing import RouteRequest
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.context_service import ContextService
from app.services.memory_service import MemoryService


class RecordingKnowledgeProvider:
    def __init__(self) -> None:
        self.requests: list[KnowledgeProviderRequest] = []

    async def retrieve(self, request: KnowledgeProviderRequest) -> KnowledgeProviderResult:
        self.requests.append(request)
        citation = KnowledgeCitation(source_id="policy_docs")
        return KnowledgeProviderResult(
            status="ok",
            trace_id="ktrace_provider_contract",
            items=[
                KnowledgeContextItem(
                    item_id="refund-policy",
                    source_id="policy_docs",
                    content="Refunds are available within 30 days.",
                    score=0.97,
                    citation=citation,
                )
            ],
            citations=[citation],
        )


def _knowledge_agent(summarizer_agent) -> AgentDefinitionV2:
    payload = summarizer_agent.model_dump(mode="json")
    payload["context"] = {
        "knowledge": {
            "mode": "prefetch",
            "source_ids": ["policy_docs"],
            "source_tags": ["public"],
            "max_items": 3,
        }
    }
    return AgentDefinitionV2.model_validate(payload)


def _memory_service(settings: Settings) -> MemoryService:
    return MemoryService(settings=settings, repository=MemoryItemRepository())


def test_knowledge_provider_exposes_one_operation() -> None:
    operations = {
        name
        for name, value in vars(KnowledgeProvider).items()
        if callable(value) and not name.startswith("_")
    }

    assert operations == {"retrieve"}


async def test_agent_prefetch_uses_provider_neutral_retrieve(
    summarizer_agent,
) -> None:
    settings = Settings(storage_backend="memory")
    provider = RecordingKnowledgeProvider()
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=_memory_service(settings),
        knowledge_provider=provider,
    )
    invocation_input = {"text": "What is the refund policy?"}
    user = UserContext(id="user-1", attributes={"tenant_id": "tenant-1"})

    runtime = await service.assemble(
        agent=_knowledge_agent(summarizer_agent),
        user=user,
        session_id="session-1",
        query=invocation_input["text"],
        invocation_input=invocation_input,
    )

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.principal == user
    assert request.consumer == f"agent:{summarizer_agent.agent_id}"
    assert request.purpose == "agent_execution"
    assert request.source_ids == ["policy_docs"]
    assert request.source_tags == ["public"]
    assert request.budget.max_items == 3
    assert runtime.knowledge_context.status == "ok"
    assert runtime.knowledge_context.metadata["trace_id"] == "ktrace_provider_contract"
    assert invocation_input["knowledge_context"]["items"][0]["source_id"] == "policy_docs"


async def test_optional_agent_runs_without_configured_knowledge_provider(
    summarizer_agent,
) -> None:
    settings = Settings(storage_backend="memory")
    service = AgentContextAssemblyService(
        settings=settings,
        memory_service=_memory_service(settings),
        knowledge_provider=None,
    )
    invocation_input = {"text": "hello"}

    runtime = await service.assemble(
        agent=_knowledge_agent(summarizer_agent),
        user=UserContext(id="user-1"),
        session_id="session-1",
        query="hello",
        invocation_input=invocation_input,
    )

    assert runtime.knowledge_context.status == "disabled"
    assert runtime.knowledge_context.items == []
    assert invocation_input["knowledge_context"]["status"] == "disabled"


async def test_route_context_never_uses_knowledge_retrieval() -> None:
    settings = Settings(
        storage_backend="memory",
        context_pipeline_mode="enforced",
        context_route_knowledge_enabled=True,
        context_route_knowledge_direct_reply_enabled=True,
        context_route_knowledge_source_ids="policy_docs",
    )
    request = RouteRequest.model_validate(
        {
            "request_id": "route-without-knowledge",
            "session_id": "session-1",
            "user": {"id": "user-1"},
            "input": {"text": "refund policy"},
        }
    )

    context, projection, _ = await ContextService(settings).assemble_route_context(
        request,
        candidate_agent_ids=[],
        candidate_agents=[],
        request_id="route-without-knowledge",
    )

    outcomes = {item["provider"] for item in context.metadata["context_trace"]["provider_outcomes"]}
    assert "route_knowledge" not in outcomes
    assert all(item.get("type") != "knowledge" for item in context.evidence)
    assert "knowledge_direct_reply" not in context.metadata
    assert projection is not None
