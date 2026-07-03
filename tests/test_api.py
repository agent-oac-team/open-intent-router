from fastapi.testclient import TestClient

from app.dependencies import get_chat_history_service
from app.main import create_app
from app.repositories.memory import MemoryMessageRepository
from app.services.chat_history_service import ChatHistoryService


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
