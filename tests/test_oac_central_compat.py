import json
from pathlib import Path

import pytest

from app.core.errors import PlanBindingUnavailableError
from app.schemas.common import ArtifactRef, UserContext
from app.schemas.plans import NextAction, Plan, PlanActionResponse, PlanStep
from app.schemas.routing import RouteContext, RouteDecision, RouteResponse
from host_adapters.oac.mappers.central import (
    agent_event_to_native,
    navigation_event_to_native,
    plan_confirm_to_compat,
    project_error,
    route_request_to_native,
    route_response_to_compat,
)
from host_adapters.oac.schemas.central import (
    AgentEventCompatResponse,
    AgentEventRequest,
    CentralRouteRequest,
    CentralRouteResponse,
    NavigationEventRequest,
    PlanConfirmResponse,
)

FIXTURES = Path("tests/contract/oac_irs/irs-baseline/v1/success")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _user() -> UserContext:
    return UserContext(
        id="trusted-user",
        groups=["operator"],
        attributes={"tenant_id": "oac"},
    )


def test_frozen_central_fixtures_parse_with_optional_ticket_compatibility() -> None:
    route = _fixture("central-route")
    navigation = _fixture("central-navigation-event")
    agent = _fixture("central-agent-event")
    confirm = _fixture("central-plan-confirm")

    assert CentralRouteRequest.model_validate(route["request"]["body"])
    assert CentralRouteResponse.model_validate(route["response"]["body"])
    assert NavigationEventRequest.model_validate(navigation["request"]["body"])
    assert AgentEventRequest.model_validate(agent["request"]["body"])
    assert AgentEventCompatResponse.model_validate(agent["response"]["body"])
    assert PlanConfirmResponse.model_validate(confirm["response"]["body"])

    ticketed = dict(route["response"]["body"], execution_ticket="opaque-ticket")
    assert CentralRouteResponse.model_validate(ticketed).execution_ticket == "opaque-ticket"


def test_route_request_mapper_uses_trusted_identity_and_keeps_history_as_context() -> None:
    body = _fixture("central-route")["request"]["body"]
    body["user_id"] = "forged-user"
    body["user_tags"] = ["forged-admin"]
    body["central_chat_history"] = [{"role": "user", "content": "previous"}]

    native = route_request_to_native(CentralRouteRequest.model_validate(body), user=_user())

    assert native.user.id == "trusted-user"
    assert native.user.groups == ["operator"]
    assert native.source == "host_chat"
    assert native.frontend_context["conversation_history"][0]["content"] == "previous"


@pytest.mark.parametrize(
    ("action", "status", "agent_id"),
    [
        ("reply", "ok", None),
        ("clarify", "clarify", None),
        ("open_agent", "ok", "agent-1"),
        ("continue_agent", "ok", "agent-1"),
        ("exit_agent", "ok", None),
        ("show_plan", "ok", None),
        ("unsupported", "unsupported", None),
        ("silent", "ok", None),
    ],
)
def test_route_response_mapper_projects_all_eight_actions(action, status, agent_id) -> None:
    plan = None
    if action == "show_plan":
        plan = Plan(
            plan_id="plan-1",
            tenant_id="oac",
            user_id="trusted-user",
            session_id="session-1",
            steps=[PlanStep(step_id="step-1", agent_id="agent-1", description="do it")],
        )
    response = RouteResponse(
        request_id="request-1",
        session_id="session-1",
        assistant_message="visible message",
        decision=RouteDecision(
            status=status,
            action=action,
            target_agent_id=agent_id,
        ),
        context=RouteContext(
            relation="continue_current" if action == "continue_agent" else "new_task",
            current_agent_id=agent_id if action == "continue_agent" else None,
            candidate_agent_ids=[agent_id] if agent_id else [],
            artifact_refs=[ArtifactRef(artifact_id="a1", uri="artifact://a1")],
        ),
        plan=plan,
    )

    compat = route_response_to_compat(
        response,
        source="central_chat",
        execution_ticket="opaque" if agent_id else None,
    )

    assert compat.route.action == action
    assert compat.route.agent_id == agent_id
    assert compat.context.artifact_refs == ["a1"]
    assert bool(compat.plan) is (action == "show_plan")


def test_event_navigation_and_plan_mappers_preserve_legacy_semantics() -> None:
    navigation = NavigationEventRequest.model_validate(
        _fixture("central-navigation-event")["request"]["body"]
    )
    native_navigation = navigation_event_to_native(
        navigation, user=_user(), event_id="navigation-1"
    )
    assert native_navigation.user_id == "trusted-user"
    assert native_navigation.payload["from"] == "/home"

    agent = AgentEventRequest.model_validate(_fixture("central-agent-event")["request"]["body"])
    native_agent = agent_event_to_native(
        agent,
        user=_user(),
        run_id="run-1",
        turn_id="turn-1",
        request_id="request-1",
    )
    assert native_agent.tenant_id == "oac"
    assert native_agent.payload["artifact_refs"] == ["artifact_fixture_001"]

    plan = Plan(
        plan_id="plan-1",
        tenant_id="oac",
        user_id="trusted-user",
        session_id="session-1",
        status="running",
        current_step_id="step-1",
        state_version=3,
        next_action=NextAction(type="open_ui", route="/poster", step_id="step-1"),
        steps=[PlanStep(step_id="step-1", agent_id="agent-1", description="do it")],
    )
    projected = plan_confirm_to_compat(
        PlanActionResponse(plan_id="plan-1", status="running", current_step_id="step-1"),
        plan=plan,
    )
    assert projected.current_step == {
        "step_id": "step-1",
        "agent_id": "agent-1",
        "status": "pending",
        "description": "do it",
        "runtime_status": "running",
    }
    assert projected.state_version == 3
    assert projected.next_action and projected.next_action.type == "open_ui"


def test_error_projection_hides_internal_failures() -> None:
    assert project_error(PermissionError())[0] == 403
    assert project_error(KeyError("secret"))[0] == 404
    assert project_error(ValueError("version conflict"))[0] == 409
    status, body = project_error(RuntimeError("database password leaked"))
    assert status == 500
    assert "password" not in body.message


def test_error_projection_keeps_only_safe_reason_code() -> None:
    status, body = project_error(
        PlanBindingUnavailableError(
            "Plan Binding is unavailable",
            details={
                "reason_code": "plan_binding_revision_incompatible",
                "raw_exception": "adapter password=secret",
            },
        )
    )

    assert status == 503
    assert body.details == {"reason_code": "plan_binding_revision_incompatible"}


@pytest.mark.parametrize("reason_code", ["token_secret", "subject_alice"])
def test_error_projection_rejects_unlisted_reason_codes(reason_code: str) -> None:
    _status, body = project_error(
        PlanBindingUnavailableError(
            "Plan Binding is unavailable",
            details={"reason_code": reason_code},
        )
    )

    assert body.details == {}
