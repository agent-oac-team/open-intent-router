from app.core.config import Settings
from app.repositories.context_stores import KnowledgeRepository, MemoryItemRepository
from app.schemas.agent_context import (
    KnowledgeCitation,
    KnowledgeContext,
    KnowledgeContextItem,
    MemoryContext,
)
from app.schemas.knowledge import KnowledgeSearchResponse
from app.schemas.memory import MemoryRecallResponse
from app.schemas.routing import (
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.services.context_service import ContextService
from app.services.router_service import RouterService


def _request() -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "request_retrieval",
            "session_id": "session_retrieval",
            "user": {
                "id": "user_retrieval",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant_retrieval"},
            },
            "input": {"text": "What is the refund policy?"},
        }
    )


async def test_route_memory_and_knowledge_are_disabled_by_default_and_explicitly_enabled() -> None:
    memory = StubMemoryService()
    knowledge = StubKnowledgeService(status="empty")
    disabled = ContextService(
        Settings(context_pipeline_mode="observe", memory_mode="off"),
        memory_service=memory,
        knowledge_service=knowledge,
    )

    await disabled.assemble_route_context(
        _request(),
        candidate_agent_ids=[],
        candidate_agents=[],
        request_id="request_retrieval",
    )

    assert memory.calls == 0
    assert knowledge.calls == 0

    enabled = ContextService(
        Settings(
            context_pipeline_mode="observe",
            memory_mode="on",
            context_route_knowledge_enabled=True,
            context_route_knowledge_source_ids="policy_docs",
        ),
        memory_service=memory,
        knowledge_service=knowledge,
    )
    context, _, _ = await enabled.assemble_route_context(
        _request(),
        candidate_agent_ids=[],
        candidate_agents=[],
        request_id="request_retrieval",
    )

    assert memory.calls == 1
    assert knowledge.calls == 1
    outcomes = {
        item["provider"]: item["status"]
        for item in context.metadata["context_trace"]["provider_outcomes"]
    }
    assert outcomes["route_memory"] == "empty"
    assert outcomes["route_knowledge"] == "empty"


async def test_reliable_route_knowledge_uses_existing_reply_contract(
    settings,
    registry_service,
) -> None:
    knowledge = StubKnowledgeService(status="ok")
    llm = FailingLLM()
    configured = settings.model_copy(
        update={
            "context_pipeline_mode": "enforced",
            "context_route_knowledge_enabled": True,
            "context_route_knowledge_direct_reply_enabled": True,
            "context_route_knowledge_source_ids": "policy_docs",
            "context_route_knowledge_min_score": 0.8,
        }
    )
    service = RouterService(
        settings=configured,
        registry=registry_service,
        llm_client=llm,
        context_service=ContextService(configured, knowledge_service=knowledge),
    )

    response = await service.route(_request())

    assert llm.calls == 0
    assert response.decision.action == "reply"
    assert response.assistant_message == "Refunds are available within 30 days."
    assert response.decision.message == response.assistant_message
    assert response.context.evidence[0]["source_id"] == "policy_docs"
    assert "citations" not in response.model_dump(mode="json")


async def test_route_knowledge_no_hit_denied_timeout_and_error_degrade_safely() -> None:
    for status in ("empty", "denied", "timeout", "error"):
        knowledge = StubKnowledgeService(status=status)
        service = ContextService(
            Settings(
                context_pipeline_mode="enforced",
                context_route_knowledge_enabled=True,
                context_route_knowledge_source_ids="policy_docs",
            ),
            knowledge_service=knowledge,
        )

        context, projection, _ = await service.assemble_route_context(
            _request(),
            candidate_agent_ids=[],
            candidate_agents=[],
            request_id="request_retrieval",
        )

        outcomes = {
            item["provider"]: item["status"]
            for item in context.metadata["context_trace"]["provider_outcomes"]
        }
        assert outcomes["route_knowledge"] == status
        assert projection is not None
        assert "Refunds are available" not in str(projection.payload)


class StubMemoryService:
    def __init__(self) -> None:
        self.calls = 0
        self.repository = MemoryItemRepository()

    async def recall(self, request):
        self.calls += 1
        return MemoryRecallResponse(context=MemoryContext(status="empty"))


class StubKnowledgeService:
    def __init__(self, *, status: str) -> None:
        self.calls = 0
        self.status = status
        self.repository = KnowledgeRepository()

    async def search(self, request):
        self.calls += 1
        if self.status == "ok":
            citation = KnowledgeCitation(
                source_id="policy_docs",
                chunk_id="refunds",
                title="Refund Policy",
                uri="https://example.test/refunds",
            )
            item = KnowledgeContextItem(
                item_id="refunds",
                source_id="policy_docs",
                content="Refunds are available within 30 days.",
                score=0.95,
                citation=citation,
            )
            return KnowledgeSearchResponse(
                context=KnowledgeContext(
                    summary=item.content,
                    items=[item],
                    citations=[citation],
                    source_ids=["policy_docs"],
                    status="ok",
                ),
                selected_source_ids=["policy_docs"],
            )
        if self.status == "denied":
            return KnowledgeSearchResponse(
                context=KnowledgeContext(status="empty"),
                denied_source_ids=["policy_docs"],
            )
        return KnowledgeSearchResponse(
            context=KnowledgeContext(
                status=self.status,
                errors=[f"knowledge_{self.status}"],
            ),
            errors=[f"knowledge_{self.status}"],
        )


class FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        self.calls += 1
        return RouteResponse(
            request_id=payload.request.request_id or "unexpected",
            session_id=payload.request.session_id,
            decision=RouteDecision(action="unsupported"),
            context=RouteContext(candidate_agent_ids=[]),
        )
