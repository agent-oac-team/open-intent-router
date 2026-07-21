from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.repositories.execution_tickets import MemoryExecutionTicketStore
from app.schemas.delegated_runs import (
    DelegatedRunCommandResult,
    DelegatedRunReference,
    DelegatedRunStatus,
)
from app.schemas.events import AgentEventResponse
from app.schemas.plans import Plan, PlanActionResponse, PlanStep
from app.schemas.routing import RouteContext, RouteDecision, RouteResponse
from app.schemas.turns import CanonicalTurn, TurnUserInput
from app.services.execution_ticket_service import ExecutionTicketService
from host_adapters.oac.api.central import router
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.cutover import CutoverGuard, MemoryCutoverAuditRepository
from host_adapters.oac.fallback.circuit import CircuitBreaker
from host_adapters.oac.fallback.gateway import IRSFallbackGateway
from host_adapters.oac.identity.models import TrustedHostIdentity
from host_apps.oac.config import OacHostSettings, get_oac_host_settings
from host_apps.oac.dependencies import (
    get_cutover_guard,
    get_execution_ticket_service,
    get_irs_fallback_gateway,
    get_irs_legacy_client,
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)


class RoutingPort:
    def __init__(self, action: str = "open_agent") -> None:
        self.action = action
        self.last_request = None

    async def route(self, request):
        self.last_request = request
        agent_id = "agent-1" if self.action in {"open_agent", "continue_agent"} else None
        plan = None
        if self.action == "show_plan":
            plan = Plan(
                plan_id="plan-1",
                tenant_id="oac",
                user_id="trusted-user",
                session_id=request.session_id,
                steps=[PlanStep(step_id="step-1", agent_id="agent-1", description="do it")],
            )
        return RouteResponse(
            request_id=request.request_id or "request-1",
            session_id=request.session_id,
            decision=RouteDecision(
                status=(
                    "clarify"
                    if self.action == "clarify"
                    else "unsupported"
                    if self.action == "unsupported"
                    else "ok"
                ),
                action=self.action,
                target_agent_id=agent_id,
                message=self.action,
            ),
            context=RouteContext(
                relation="continue_current" if self.action == "continue_agent" else "new_task",
                current_agent_id="agent-1" if self.action == "continue_agent" else None,
                candidate_agent_ids=[agent_id] if agent_id else [],
            ),
            plan=plan,
        )


class TurnPort:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.turn = CanonicalTurn(
            turn_id="turn-1",
            tenant_id="oac",
            user_id="trusted-user",
            session_id="session-1",
            request_id="request-1",
            source="host_chat",
            user_input=TurnUserInput(text="hello"),
            created_at=now,
            updated_at=now,
        )

    async def start_turn(self, **kwargs):
        return SimpleNamespace(turn=self.turn, created=True)

    async def attach_activity(self, **kwargs):
        return self.turn

    async def submission_status(self, **kwargs):
        return "not_accepted"


class DelegatedPort:
    def __init__(self) -> None:
        self.started = None
        self.completed = None
        self.completed_events = set()

    async def start(self, command):
        self.started = command
        return DelegatedRunCommandResult(
            run=DelegatedRunReference(
                run_id="run-1",
                turn_id=command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
                agent_id=command.agent_id,
                status=DelegatedRunStatus.RUNNING,
                state_version=1,
                deadline_at=command.deadline_at,
            ),
            turn_id=command.turn_id,
        )

    async def progress(self, command):
        return DelegatedRunCommandResult(
            run=DelegatedRunReference(
                run_id=command.run_id,
                turn_id=command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
                agent_id=command.agent_id,
                status=DelegatedRunStatus.RUNNING,
                state_version=command.expected_state_version + 1,
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            ),
            turn_id=command.turn_id,
        )

    async def complete(self, command):
        duplicate = command.event_id in self.completed_events
        self.completed_events.add(command.event_id)
        self.completed = command
        return DelegatedRunCommandResult(
            run=DelegatedRunReference(
                run_id=command.run_id,
                turn_id=command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
                agent_id=command.agent_id,
                status=DelegatedRunStatus.COMPLETED,
                state_version=command.expected_state_version + 1,
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            ),
            result_id=command.result_id,
            turn_id=command.turn_id,
            duplicate=duplicate,
        )


class EventPort:
    def __init__(self) -> None:
        self.navigation = []

    async def record_conversation_event(self, event):
        self.navigation.append(event)
        return event

    async def record_agent_event(self, event):
        return AgentEventResponse(event_id=event.event_id)


class PlanPort:
    def __init__(self) -> None:
        self.plan = Plan(
            plan_id="plan-1",
            tenant_id="oac",
            user_id="trusted-user",
            session_id="session-1",
            steps=[PlanStep(step_id="step-1", agent_id="agent-1", description="do it")],
        )

    async def get_plan(self, plan_id, *, tenant_id, user_id):
        return self.plan if (tenant_id, user_id) == ("oac", "trusted-user") else None

    async def confirm(self, plan_id, *, tenant_id, user_id, publish=True):
        return PlanActionResponse(
            plan_id=plan_id,
            status="running",
            current_step_id="step-1",
        )


def _client(*, user_id: str = "trusted-user", action: str = "open_agent"):
    delegated = DelegatedPort()
    events = EventPort()
    ports = OacAdapterApplicationPorts(
        routing=RoutingPort(action),
        knowledge=SimpleNamespace(),
        knowledge_assets=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=events,
        plans=PlanPort(),
        delegated_runs=delegated,
        turns=TurnPort(),
    )
    tickets = ExecutionTicketService(MemoryExecutionTicketStore(), secret="test-secret")
    identity = TrustedHostIdentity(
        key_id="key-1",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id=user_id,
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=("operator",),
        active_bundle_id="oac-operations",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="route",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_execution_ticket_service] = lambda: tickets
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity
    app.dependency_overrides[get_oac_host_settings] = lambda: OacHostSettings(
        _env_file=None,
        execution_ticket_secret="test-secret",
        execution_ticket_ttl_seconds=300,
        execution_ticket_lease_seconds=30,
    )
    return TestClient(app), delegated, events


def test_route_issues_ticket_and_completed_event_consumes_it() -> None:
    client, delegated, _ = _client()
    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-1",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )
    assert route.status_code == 200
    ticket = route.json()["execution_ticket"]
    assert ticket
    assert delegated.started.user_id == "trusted-user"

    event = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "event-1",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "status": "completed",
            "event_type": "agent_result",
            "message": "done",
            "execution_ticket": ticket,
        },
    )
    assert event.status_code == 200
    assert event.json() == {
        "event_id": "event-1",
        "session_id": "session-1",
        "accepted": True,
        "duplicate": False,
        "route_required": True,
    }
    assert delegated.completed.turn_id == "turn-1"


def test_pre_cutover_agent_event_is_quarantined_before_core_mutation() -> None:
    client, delegated, _ = _client()
    repository = MemoryCutoverAuditRepository()
    watermark = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    client.app.dependency_overrides[get_cutover_guard] = lambda: CutoverGuard(
        watermark=watermark,
        repository=repository,
    )

    response = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "old-event-1",
            "session_id": "old-session-1",
            "agent_id": "agent-1",
            "status": "completed",
            "message": "must not enter core",
            "created_at": "2026-07-16T11:59:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["route_required"] is False
    assert delegated.completed is None
    assert len(repository.records) == 1
    serialized = str(repository.records[0])
    assert "old-session-1" not in serialized
    assert "must not enter core" not in serialized


def test_navigation_and_plan_confirm_keep_legacy_status_and_shape() -> None:
    client, _, events = _client()
    navigation = client.post(
        "/api/v1/central/events/navigation",
        json={
            "session_id": "session-1",
            "user_id": "forged-user",
            "from": "/home",
            "to": "/customer",
            "reason": "test",
        },
    )
    assert navigation.status_code == 202
    assert navigation.json() == {"accepted": True}
    assert events.navigation[0].user_id == "trusted-user"

    confirm = client.post("/api/v1/central/plans/plan-1/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["current_step"]["runtime_status"] == "running"
    duplicate = client.post("/api/v1/central/plans/plan-1/confirm")
    assert duplicate.status_code == 200
    assert duplicate.json() == confirm.json()


@pytest.mark.parametrize(
    ("commit_status", "expected_status", "fallback_calls"),
    [("not_accepted", 200, 1), ("unknown", 503, 0), ("committed", 503, 0)],
)
def test_route_fallback_requires_not_accepted_proof(
    commit_status, expected_status, fallback_calls
) -> None:
    client, _, _ = _client(action="reply")
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    ports.turns.submission_status = _submission_status(commit_status)
    ports = replace(ports, routing=FailingRoutingPort())
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    legacy = StubIRSRouteClient()
    client.app.dependency_overrides[get_irs_fallback_gateway] = lambda: IRSFallbackGateway(
        mode="safe_route",
        policy_version="test-policy",
        circuit=CircuitBreaker(failure_threshold=1, recovery_seconds=30),
    )
    client.app.dependency_overrides[get_irs_legacy_client] = lambda: legacy

    response = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-fallback",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )

    assert response.status_code == expected_status
    assert len(legacy.calls) == fallback_calls
    if commit_status == "unknown":
        assert response.json()["detail"]["reason"] == "ambiguous_commit"


class FailingRoutingPort:
    async def route(self, _request):
        raise TimeoutError("route timeout")


def _submission_status(status):
    async def resolve(**kwargs):
        del kwargs
        return status

    return resolve


class StubIRSRouteClient:
    def __init__(self) -> None:
        self.calls = []

    async def request_json(self, *, method, path, json_body=None, query=None):
        del query
        self.calls.append((method, path))
        return {
            "request_id": json_body["request_id"],
            "session_id": json_body["session_id"],
            "route": {
                "status": "ok",
                "action": "reply",
                "agent_id": None,
                "message": "legacy",
            },
            "context": {
                "source": json_body["source"],
                "current_agent_id": None,
                "relation": "new_task",
                "artifact_refs": [],
            },
            "plan": None,
        }

    other_client, _, _ = _client(user_id="other-user")
    rejected = other_client.post("/api/v1/central/plans/plan-1/confirm")
    assert rejected.status_code == 404


def test_legacy_agent_event_without_ticket_requires_unique_server_mapping() -> None:
    client, delegated, _ = _client()
    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-1",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )
    assert route.status_code == 200

    event = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "event-legacy",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "status": "completed",
            "message": "done without ticket",
        },
    )
    assert event.status_code == 200
    assert delegated.completed.event_id == "event-legacy"


@pytest.mark.parametrize(
    ("action", "expects_ticket"),
    [
        ("reply", False),
        ("clarify", False),
        ("open_agent", True),
        ("continue_agent", True),
        ("exit_agent", False),
        ("show_plan", False),
        ("unsupported", False),
        ("silent", False),
    ],
)
def test_central_route_e2e_covers_all_legacy_actions(action, expects_ticket) -> None:
    client, _, _ = _client(action=action)
    body = {
        "request_id": "request-1",
        "session_id": "session-1",
        "user_id": "trusted-user",
        "user_tags": ["运营版"],
        "source": "agent_chat" if action == "continue_agent" else "central_chat",
        "user_query": "hello",
    }
    if action == "continue_agent":
        body["current_agent_id"] = "agent-1"
    response = client.post("/api/v1/central/route", json=body)
    assert response.status_code == 200
    assert response.json()["route"]["action"] == action
    assert bool(response.json().get("execution_ticket")) is expects_ticket
    assert bool(response.json().get("plan")) is (action == "show_plan")


def test_completed_agent_event_retry_returns_duplicate_without_second_effect() -> None:
    client, _, _ = _client()
    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-1",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )
    payload = {
        "event_id": "event-retry",
        "session_id": "session-1",
        "agent_id": "agent-1",
        "status": "completed",
        "message": "done",
        "execution_ticket": route.json()["execution_ticket"],
    }
    first = client.post("/api/v1/central/events/agent", json=payload)
    second = client.post("/api/v1/central/events/agent", json=payload)
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


def test_v2_route_projects_bundle_entitlement_and_rejects_body_mismatch() -> None:
    client, _, _ = _client()
    identity = TrustedHostIdentity(
        key_id="key-v2",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id="trusted-user",
        groups=(),
        credential_class="oac_user",
        claims_version="oac-principal-v1",
        roles=("operator",),
        active_bundle_id="oac-operations",
        policy_version="oac-authz-v1",
    )
    client.app.dependency_overrides[get_trusted_host_identity] = lambda: identity
    client.app.dependency_overrides[get_oac_host_settings] = lambda: OacHostSettings(
        _env_file=None,
        execution_ticket_secret="test-secret",
    )
    body = {
        "request_id": "request-v2",
        "session_id": "session-1",
        "user_id": "trusted-user",
        "user_tags": ["运营版"],
        "source": "central_chat",
        "user_query": "hello",
    }
    response = client.post("/api/v1/central/route", json=body)
    assert response.status_code == 200
    routing = client.app.dependency_overrides[get_oac_adapter_application_ports]().routing
    assert routing.last_request.user.roles == ["operator"]
    assert routing.last_request.user.entitlements == ["workspace.operations.access"]

    mismatch = client.post(
        "/api/v1/central/route",
        json={**body, "request_id": "mismatch", "user_tags": ["展业版"]},
    )
    assert mismatch.status_code == 403
    assert mismatch.json() == {"detail": "host_claims_mismatch"}


def test_central_route_v2_gate_rejects_v1_identity() -> None:
    client, _, _ = _client()
    client.app.dependency_overrides[get_trusted_host_identity] = lambda: TrustedHostIdentity(
        key_id="legacy-key",
        audience="test",
        principal_type="user",
        tenant_id="oac",
        user_id="trusted-user",
        groups=("operator",),
        credential_class="oac_user",
    )
    client.app.dependency_overrides[get_oac_host_settings] = lambda: OacHostSettings(
        _env_file=None,
        execution_ticket_secret="test-secret",
    )
    response = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-v1",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "host_authentication_failed"}
