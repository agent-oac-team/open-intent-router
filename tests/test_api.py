from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import memory_identity_signature
from app.dependencies import (
    get_chat_history_service,
    get_memory_observability_service,
    get_memory_service,
    get_router_service,
)
from app.main import create_app
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory import MemoryMessageRepository
from app.schemas.memory import MemoryItem
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
from app.services.chat_history_service import ChatHistoryService
from app.services.memory_service import MemoryService
from tests.fakes.native_principal import native_principal_headers


def test_health_endpoint() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_append_session_message_endpoint_stores_agent_reply(non_lifespan_test_client) -> None:
    repository = MemoryMessageRepository()
    service = ChatHistoryService(repository, host_limit=20, agent_limit=12)
    app = create_app()
    app.dependency_overrides[get_chat_history_service] = lambda: service
    client = non_lifespan_test_client(app)

    response = client.post(
        "/api/v1/sessions/s1/messages",
        headers=native_principal_headers(
            secret="", subject="local-user", tenant="local-tenant", signed=False
        ),
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


def test_append_session_message_endpoint_validates_agent_chat_agent_id(
    non_lifespan_test_client,
) -> None:
    app = create_app()
    app.dependency_overrides[get_chat_history_service] = lambda: ChatHistoryService(
        MemoryMessageRepository(),
        host_limit=20,
        agent_limit=12,
    )
    client = non_lifespan_test_client(app)

    response = client.post(
        "/api/v1/sessions/s1/messages",
        headers=native_principal_headers(
            secret="", subject="local-user", tenant="local-tenant", signed=False
        ),
        json={"source": "agent_chat", "role": "agent", "content": "child reply"},
    )

    assert response.status_code == 422


def test_route_endpoint_preserves_response_contract_shape(non_lifespan_test_client) -> None:
    app = create_app()
    service = ContractRouterService()
    app.dependency_overrides[get_router_service] = lambda: service
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        memory_identity_secret="route-test-secret",
    )
    client = non_lifespan_test_client(app)

    headers = {
        "X-User-ID": "trusted-user",
        "X-Tenant-ID": "trusted-tenant",
        "X-Memory-Identity-Signature": memory_identity_signature(
            user_id="trusted-user",
            tenant_id="trusted-tenant",
            secret="route-test-secret",
        ),
    }
    response = client.post(
        "/api/v1/route",
        headers=headers,
        json={
            "request_id": "req_contract",
            "session_id": "s1",
            "user": {
                "id": "trusted-user",
                "roles": ["operator"],
                "attributes": {"tenant_id": "trusted-tenant"},
            },
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
    assert service.last_payload.user.id == "trusted-user"
    assert service.last_payload.user.tenant_id == "trusted-tenant"
    assert service.last_payload.user.roles == []

    unsigned = client.post(
        "/api/v1/route",
        headers={"X-User-ID": "trusted-user", "X-Tenant-ID": "trusted-tenant"},
        json={
            "session_id": "s1",
            "user": {"id": "trusted-user", "attributes": {"tenant_id": "trusted-tenant"}},
            "input": {"text": "summarize this text"},
        },
    )
    assert unsigned.status_code == 401


async def test_memory_debug_endpoint_returns_admin_state(non_lifespan_test_client) -> None:
    memory_repository = MemoryItemRepository()
    await memory_repository.add(
        MemoryItem(
            scope="user_preference", subject_id="u1", user_id="u1", content="prefers concise"
        )
    )
    app = create_app()
    app.dependency_overrides[get_memory_service] = lambda: MemoryService(
        settings=Settings(storage_backend="memory"),
        repository=memory_repository,
    )
    app.dependency_overrides[get_memory_observability_service] = object
    client = non_lifespan_test_client(app)

    memory_response = client.get("/api/v1/memories/debug?user_id=u1")

    assert memory_response.status_code == 200
    assert memory_response.json()["metadata"]["item_count"] == 1


class ContractRouterService:
    last_payload: RouteRequest

    async def route(self, payload: RouteRequest) -> RouteResponse:
        self.last_payload = payload
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
