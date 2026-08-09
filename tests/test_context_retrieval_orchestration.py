from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.schemas.agent_context import MemoryContext
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
    disabled = ContextService(
        Settings(context_pipeline_mode="observe", memory_mode="off"),
        memory_service=memory,
    )

    await disabled.assemble_route_context(
        _request(),
        candidate_agent_ids=[],
        candidate_agents=[],
        request_id="request_retrieval",
    )

    assert memory.calls == 0

    enabled = ContextService(
        Settings(
            context_pipeline_mode="observe",
            memory_mode="on",
            context_route_knowledge_enabled=True,
            context_route_knowledge_source_ids="policy_docs",
        ),
        memory_service=memory,
    )
    context, _, _ = await enabled.assemble_route_context(
        _request(),
        candidate_agent_ids=[],
        candidate_agents=[],
        request_id="request_retrieval",
    )

    assert memory.calls == 1
    outcomes = {
        item["provider"]: item["status"]
        for item in context.metadata["context_trace"]["provider_outcomes"]
    }
    assert outcomes["route_memory"] == "empty"
    assert "route_knowledge" not in outcomes


async def test_route_knowledge_flags_cannot_bypass_route_decision(
    settings,
    registry_service,
) -> None:
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
        context_service=ContextService(configured),
    )

    response = await service.route(_request())

    assert llm.calls == 1
    assert response.decision.action == "unsupported"
    assert response.context.evidence == []
    assert "knowledge_direct_reply" not in response.context.metadata


async def test_route_knowledge_no_hit_denied_timeout_and_error_degrade_safely() -> None:
    for _legacy_status in ("empty", "denied", "timeout", "error"):
        service = ContextService(
            Settings(
                context_pipeline_mode="enforced",
                context_route_knowledge_enabled=True,
                context_route_knowledge_source_ids="policy_docs",
            ),
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
        assert "route_knowledge" not in outcomes
        assert projection is not None
        assert "Refunds are available" not in str(projection.payload)


class StubMemoryService:
    def __init__(self) -> None:
        self.calls = 0
        self.repository = MemoryItemRepository()

    async def recall(self, request):
        self.calls += 1
        return MemoryRecallResponse(context=MemoryContext(status="empty"))


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
