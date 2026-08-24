import json

from app.prompts.router_prompt import RouterPromptTemplate
from app.schemas.agents import CandidateAgentV2
from app.schemas.context import ContextProjection
from app.schemas.routing import LLMRouteInput, RouteContext, RouteRequest


def _request() -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "req_projection",
            "session_id": "session_projection",
            "user": {
                "id": "user_projection",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant_1", "secret": "request-secret"},
            },
            "input": {
                "text": "selected current input",
                "attachments": [{"name": "private", "content": "attachment-secret"}],
            },
            "frontend_context": {
                "page": "selected page",
                "unselected": "frontend-secret",
            },
        }
    )


def _candidate() -> CandidateAgentV2:
    return CandidateAgentV2(
        agent_id="summarizer",
        name="Summarizer",
        description="Summarize text",
    )


def _projection() -> ContextProjection:
    return ContextProjection(
        projection_id="projection_1",
        pack_id="pack_1",
        request_id="req_projection",
        session_id="session_projection",
        purpose="route_decision",
        consumer="router",
        payload={
            "request": {
                "request_id": "req_projection",
                "session_id": "session_projection",
                "source": "host_chat",
                "user": {"id": "user_projection", "roles": ["operator"], "tenant_id": "tenant_1"},
            },
            "context": {
                "items": [
                    {
                        "item_id": "current_input",
                        "source": "current_input",
                        "content": "selected current input",
                    }
                ]
            },
        },
        rendered_chars=240,
        token_estimate=60,
        projection_hash="projection-hash",
        projection_version="v1",
    )


def test_enforced_prompt_uses_only_projection_not_raw_request_or_debug_context() -> None:
    context = RouteContext(
        candidate_agent_ids=["summarizer"],
        metadata={
            "host_history": [{"content": "dropped-history-secret"}],
            "frontend_context": {"unselected": "frontend-secret"},
            "context_pack": {
                "selection": [{"item_id": "dropped", "drop_reason": "budget"}],
                "items": [
                    {
                        "item_id": "dropped",
                        "content": "debug-pack-secret",
                        "structured_value": {"raw": "structured-secret"},
                    }
                ],
            },
        },
        evidence=[{"content": "dropped-evidence-secret"}],
    )
    payload = LLMRouteInput(
        request=_request(),
        candidates=[_candidate()],
        context=context,
        projection=_projection(),
    )

    prompt = RouterPromptTemplate().messages(payload)[1]["content"]

    assert "selected current input" in prompt
    for forbidden in (
        "request-secret",
        "attachment-secret",
        "frontend-secret",
        "dropped-history-secret",
        "dropped-evidence-secret",
        "debug-pack-secret",
        "structured-secret",
        '"context_pack"',
    ):
        assert forbidden not in prompt


def test_projection_payload_is_the_only_context_serialized() -> None:
    payload = LLMRouteInput(
        request=_request(),
        candidates=[_candidate()],
        context=RouteContext(metadata={"dropped_metadata": "metadata-secret"}),
        projection=_projection(),
    )

    prompt = RouterPromptTemplate().messages(payload)[1]["content"]
    payload_json = json.loads(prompt.split("输入数据：\n", 1)[1].split("\n\n输出要求", 1)[0])

    assert payload_json["request"] == _projection().payload["request"]
    assert payload_json["context"] == _projection().payload["context"]
    assert "dropped_metadata" not in prompt
