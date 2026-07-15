from typing import get_args

import pytest

from app.schemas.common import RouteAction, normalize_artifact_refs
from app.schemas.plans import Plan
from app.schemas.routing import RouteDecision, RouteRequest, RouteResponse


def test_artifact_refs_accept_strings() -> None:
    refs = normalize_artifact_refs(["artifact_1"])
    assert refs[0].artifact_id == "artifact_1"
    assert refs[0].uri == "artifact_1"


def test_route_request_requires_current_agent_for_agent_chat() -> None:
    with pytest.raises(ValueError):
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "source": "agent_chat",
                "user": {"id": "u1"},
                "input": {"text": "continue"},
            }
        )


def test_route_response_rejects_target_outside_candidates() -> None:
    with pytest.raises(ValueError):
        RouteResponse(
            request_id="r1",
            session_id="s1",
            decision=RouteDecision(action="open_agent", target_agent_id="missing"),
            context={"relation": "new_task", "candidate_agent_ids": ["summarizer"]},
        )


def test_route_action_set_covers_m3_contract() -> None:
    assert set(get_args(RouteAction)) == {
        "reply",
        "clarify",
        "open_agent",
        "continue_agent",
        "exit_agent",
        "show_plan",
        "unsupported",
        "silent",
    }
    RouteDecision(action="reply")
    RouteDecision(action="clarify")
    RouteDecision(action="open_agent", target_agent_id="summarizer")
    RouteDecision(action="continue_agent", target_agent_id="summarizer")
    RouteDecision(action="exit_agent")
    RouteDecision(action="show_plan")
    RouteDecision(action="unsupported")
    RouteDecision(action="silent")


def test_route_response_accepts_assistant_message() -> None:
    response = RouteResponse(
        request_id="r1",
        session_id="s1",
        assistant_message="已路由。",
        decision=RouteDecision(action="reply", message="兼容文案"),
        context={"relation": "new_task", "candidate_agent_ids": []},
    )

    assert response.assistant_message == "已路由。"


def test_plan_rejects_dependency_cycles() -> None:
    with pytest.raises(ValueError):
        Plan.model_validate(
            {
                "plan_id": "p1",
                "user_id": "u1",
                "tenant_id": "t1",
                "steps": [
                    {"step_id": "a", "agent_id": "x", "description": "a", "depends_on": ["b"]},
                    {"step_id": "b", "agent_id": "x", "description": "b", "depends_on": ["a"]},
                ],
            }
        )


def test_plan_requires_non_empty_server_ownership() -> None:
    with pytest.raises(ValueError):
        Plan.model_validate(
            {
                "plan_id": "p1",
                "user_id": "",
                "tenant_id": "",
                "steps": [{"step_id": "s1", "agent_id": "x", "description": "step"}],
            }
        )
