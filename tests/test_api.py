from fastapi.testclient import TestClient

from app.core.config import Settings
from app.dependencies import (
    get_chat_history_service,
    get_knowledge_service,
    get_memory_service,
    get_router_service,
)
from app.main import create_app
from app.repositories.context_stores import KnowledgeRepository, MemoryItemRepository
from app.repositories.memory import MemoryMessageRepository
from app.schemas.knowledge import KnowledgeChunk, KnowledgeSource
from app.schemas.memory import MemoryItem
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
from app.services.chat_history_service import ChatHistoryService
from app.services.knowledge_service import KnowledgeService
from app.services.memory_service import MemoryService


def test_health_endpoint() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_append_session_message_endpoint_stores_agent_reply() -> None:
    repository = MemoryMessageRepository()
    service = ChatHistoryService(repository, host_limit=20, agent_limit=12)
    app = create_app()
    app.dependency_overrides[get_chat_history_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        "/api/v1/sessions/s1/messages",
        json={
            "source": "agent_chat",
            "role": "agent",
            "content": "child reply",
            "agent_id": "summarizer",
            "agent_session_id": "child_session",
            "request_id": "req_1",
            "metadata": {"trace_id": "trace_1"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s1"
    assert body["source"] == "agent_chat"
    assert body["agent_id"] == "summarizer"
    assert body["agent_session_id"] == "child_session"


def test_append_session_message_endpoint_validates_agent_chat_agent_id() -> None:
    app = create_app()
    app.dependency_overrides[get_chat_history_service] = lambda: ChatHistoryService(
        MemoryMessageRepository(),
        host_limit=20,
        agent_limit=12,
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/sessions/s1/messages",
        json={"source": "agent_chat", "role": "agent", "content": "child reply"},
    )

    assert response.status_code == 422


def test_route_endpoint_preserves_response_contract_shape() -> None:
    app = create_app()
    app.dependency_overrides[get_router_service] = lambda: ContractRouterService()
    client = TestClient(app)

    response = client.post(
        "/api/v1/route",
        json={
            "request_id": "req_contract",
            "session_id": "s1",
            "user": {"id": "u1", "roles": ["operator"]},
            "input": {"text": "summarize this text"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "request_id",
        "session_id",
        "assistant_message",
        "decision",
        "context",
        "execution_policy",
        "next_action",
        "plan",
        "invocation",
        "error",
    }
    assert body["request_id"] == "req_contract"
    assert body["decision"]["action"] == "open_agent"
    assert body["decision"]["target_agent_id"] == "summarizer"
    assert body["context"]["candidate_agent_ids"] == ["summarizer"]
    assert body["invocation"]["mode"] == "deferred"
    assert body["invocation"]["agent_id"] == "summarizer"


async def test_memory_and_knowledge_debug_endpoints_return_admin_state() -> None:
    memory_repository = MemoryItemRepository()
    await memory_repository.add(
        MemoryItem(
            scope="user_preference", subject_id="u1", user_id="u1", content="prefers concise"
        )
    )
    knowledge_repository = KnowledgeRepository()
    await knowledge_repository.upsert_source(KnowledgeSource(source_id="docs", name="Docs"))
    await knowledge_repository.add_chunk(KnowledgeChunk(source_id="docs", content="risk guide"))
    app = create_app()
    app.dependency_overrides[get_memory_service] = lambda: MemoryService(
        settings=Settings(storage_backend="memory"),
        repository=memory_repository,
    )
    app.dependency_overrides[get_knowledge_service] = lambda: KnowledgeService(
        settings=Settings(storage_backend="memory"),
        repository=knowledge_repository,
    )
    client = TestClient(app)

    memory_response = client.get("/api/v1/memories/debug?user_id=u1")
    knowledge_response = client.get("/api/v1/knowledge/debug?source_ids=docs")

    assert memory_response.status_code == 200
    assert memory_response.json()["metadata"]["item_count"] == 1
    assert knowledge_response.status_code == 200
    assert knowledge_response.json()["metadata"]["source_count"] == 1
    assert knowledge_response.json()["chunks"][0]["source_id"] == "docs"


class ContractRouterService:
    async def route(self, payload: RouteRequest) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request_id or "req_contract",
            session_id=payload.session_id,
            assistant_message="Routing to Summarizer.",
            decision=RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id="summarizer",
                confidence=0.91,
                reason="Contract fixture route.",
                message="Routing to Summarizer.",
            ),
            context=RouteContext(
                relation="new_task",
                candidate_agent_ids=["summarizer"],
            ),
            invocation={
                "mode": "deferred",
                "agent_id": "summarizer",
                "input": {"text": payload.input.text},
            },
        )
