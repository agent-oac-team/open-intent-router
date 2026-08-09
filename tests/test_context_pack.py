from app.core.config import Settings
from app.core.memory_runtime import build_memory_runtime_policy
from app.schemas.logs import AgentResult
from app.schemas.routing import (
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.schemas.sessions import AppendChatMessageRequest
from app.services.chat_history_service import ChatHistoryService
from app.services.context_service import ContextService, token_estimate_for_text
from app.services.router_service import RouterService


async def test_context_pack_includes_all_items_when_budget_allows(settings) -> None:
    service = ContextService(settings)
    request = RouteRequest.model_validate(
        {
            "session_id": "s1",
            "user": {"id": "u1"},
            "input": {"text": "current question"},
            "context_budget": {"max_tokens": 1000},
        }
    )

    pack = service.build_context_pack(
        request,
        request_id="req_1",
        host_history=[
            {
                "message_id": "m1",
                "session_id": "s1",
                "source": "host_chat",
                "role": "assistant",
                "content": "old assistant answer",
            }
        ],
        evidence=[{"id": "ev1", "content": "evidence snippet", "score": 0.8}],
    )

    assert pack.usage.included_count == len(pack.items)
    assert pack.usage.dropped_count == 0
    assert {item.source for item in pack.items} >= {"current_input", "host_history", "evidence"}


async def test_context_pack_preserves_current_input_and_drops_history_when_over_budget() -> None:
    service = ContextService(
        Settings(
            storage_backend="memory",
            registry_backend="database",
            context_per_item_char_limit=500,
            context_per_item_token_limit=200,
        )
    )
    request = RouteRequest.model_validate(
        {
            "session_id": "s1",
            "user": {"id": "u1"},
            "input": {"text": "current input must stay"},
            "context_budget": {"max_tokens": 8, "per_item_char_limit": 500},
        }
    )

    pack = service.build_context_pack(
        request,
        request_id="req_1",
        host_history=[
            {
                "message_id": "m1",
                "session_id": "s1",
                "source": "host_chat",
                "role": "assistant",
                "content": "history " * 200,
            }
        ],
    )

    current = next(item for item in pack.items if item.item_id == "current_input")
    history = next(item for item in pack.items if item.source == "host_history")
    assert current.included is True
    assert history.included is False
    assert history.drop_reason == "total_budget_exceeded"
    assert pack.usage.drop_reasons["total_budget_exceeded"] == 1


async def test_context_pack_truncates_per_item_limit_and_converts_character_budget() -> None:
    service = ContextService(Settings(storage_backend="memory", registry_backend="database"))
    request = RouteRequest.model_validate(
        {
            "session_id": "s1",
            "user": {"id": "u1"},
            "input": {"text": "short"},
            "frontend_context": {
                "context_budget": {
                    "max_chars": 80,
                    "chars_per_token": 4,
                    "per_item_char_limit": 20,
                }
            },
        }
    )

    budget = service.context_budget_for_request(request)
    pack = service.build_context_pack(
        request,
        request_id="req_1",
        evidence=[{"id": "ev1", "content": "x" * 100}],
    )
    evidence = next(item for item in pack.items if item.source == "evidence")

    assert budget.max_tokens == 20
    assert evidence.truncated is True
    assert evidence.summary_placeholder is True
    assert evidence.metadata["truncation_reason"] == "per_item_char_limit"
    assert evidence.drop_reason == "total_budget_exceeded"
    assert evidence.token_estimate == token_estimate_for_text(
        evidence.content, budget.chars_per_token
    )


async def test_router_attaches_context_pack_to_llm_input_response_and_route_log(
    settings,
    registry_service,
    repositories,
) -> None:
    chat_history = ChatHistoryService(repositories["messages"], host_limit=20, agent_limit=12)
    await chat_history.append_message(
        session_id="s1",
        payload=AppendChatMessageRequest(
            source="agent_chat",
            role="agent",
            user_id="u1",
            tenant_id="t1",
            agent_id="summarizer",
            agent_session_id="child_session",
            content="child agent reply " * 120,
            metadata={"token": "secret-token"},
        ),
    )
    await repositories["results"].add_result(
        AgentResult(
            result_id="res_1",
            run_id="run_1",
            session_id="s1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            status="completed",
            output={"summary": "recent result"},
            artifact_refs=[{"artifact_id": "a1", "uri": "memory://a1"}],
        )
    )
    llm = ContextCapturingLLM()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=llm,
        context_service=ContextService(settings, runtime_policy=build_memory_runtime_policy("off")),
        chat_history_service=chat_history,
        result_repository=repositories["results"],
        route_log_repository=repositories["route_logs"],
        runtime_policy=build_memory_runtime_policy("off"),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "source": "agent_chat",
                "user": {
                    "id": "u1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
                "input": {"text": "continue summary"},
                "current_agent": {"agent_id": "summarizer", "agent_session_id": "child_session"},
                "context_budget": {"max_tokens": 80, "per_item_token_limit": 12},
            }
        )
    )

    context_pack = response.context.metadata["context_pack"]
    assert llm.context_pack["pack_id"] == context_pack["pack_id"]
    assert context_pack["usage"]["included_count"] >= 2
    assert "***REDACTED***" in str(context_pack)
    assert "secret-token" not in str(context_pack)
    assert any(item["source"] == "agent_history" for item in context_pack["selection"])
    assert any(item["source"] == "recent_result" for item in context_pack["selection"])
    assert any(
        item["source"] == "agent_history" and item["agent_session_id"] == "child_session"
        for item in context_pack["selection"]
    )

    stored_agent_history = await chat_history.get_agent_history(
        "s1", "summarizer", tenant_id="t1", user_id="u1"
    )
    assert [item.role for item in stored_agent_history] == ["agent", "user"]

    route_log = repositories["route_logs"].logs[-1]
    log_pack = route_log.parsed_output["context_pack_usage"]
    assert log_pack["usage"]["budget_tokens"] == 80
    assert "items" not in log_pack
    assert all("content" not in item for item in log_pack["selection"])
    assert "child agent reply" not in str(log_pack)
    assert "secret-token" not in str(route_log.parsed_output)


async def test_append_agent_chat_message_requires_agent_id() -> None:
    try:
        AppendChatMessageRequest(source="agent_chat", role="agent", content="reply")
    except ValueError as exc:
        assert "agent_id" in str(exc)
    else:
        raise AssertionError("agent_id validation did not run")


class ContextCapturingLLM:
    def __init__(self) -> None:
        self.context_pack = None

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        self.context_pack = payload.context.metadata["context_pack"]
        target = payload.candidates[0].agent_id
        return RouteResponse(
            request_id=payload.request.request_id or "req_context_capture",
            session_id=payload.request.session_id,
            assistant_message="Captured.",
            decision=RouteDecision(
                status="ok",
                action="continue_agent",
                target_agent_id=target,
                confidence=0.9,
                reason="Captured context pack.",
                message="Captured.",
            ),
            context=RouteContext(
                relation="continue_current",
                current_agent_id=target,
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates],
            ),
        )
