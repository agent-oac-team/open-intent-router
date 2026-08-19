import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import LLMError
from app.repositories.execution_tickets import MemoryExecutionTicketStore
from app.repositories.execution_traces import MemoryExecutionTraceRepository
from app.schemas.agents import AgentDefinition, InvocationSpec
from app.schemas.delegated_runs import (
    DelegatedRunCommandResult,
    DelegatedRunReference,
    DelegatedRunStatus,
)
from app.schemas.events import AgentEventResponse
from app.schemas.execution_traces import ExecutionTraceQuery
from app.schemas.plans import NextAction, Plan, PlanActionResponse, PlanStep
from app.schemas.routing import RouteContext, RouteDecision, RouteResponse
from app.schemas.turns import CanonicalTurn, TurnUserInput
from app.services.execution_ticket_service import ExecutionTicketService
from app.services.execution_trace_service import ExecutionTraceService
from host_adapters.oac.api.central import router
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.cutover import CutoverGuard, MemoryCutoverAuditRepository
from host_adapters.oac.identity.models import TrustedHostIdentity
from host_apps.oac.config import OacHostSettings, get_oac_host_settings
from host_apps.oac.dependencies import (
    get_cutover_guard,
    get_execution_ticket_service,
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
                state_version=4,
                next_action=NextAction(type="confirm_plan", plan_id="plan-1"),
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


class LLMFailingRoutingPort(RoutingPort):
    async def route(self, request):
        raise LLMError("routing provider unavailable")


class ContextRoutingPort(RoutingPort):
    async def route(self, request):
        response = await super().route(request)
        return response.model_copy(
            update={
                "context": response.context.model_copy(
                    update={
                        "metadata": {
                            "context_pack": {
                                "consumer": "router",
                                "usage": {"included_count": 3, "dropped_count": 2},
                                "selection": [
                                    {
                                        "source": "memory",
                                        "scope": "user_preference",
                                        "included": True,
                                    },
                                    {
                                        "source": "memory",
                                        "scope": "user_preference",
                                        "included": False,
                                    },
                                ],
                                "provider_outcomes": [
                                    {"provider": "memory", "status": "timeout"},
                                ],
                                "items": [{"content": "must never reach the trace"}],
                            }
                        }
                    }
                )
            }
        )


class UiHandoffRoutingPort(RoutingPort):
    async def route(self, request):
        return (await super().route(request)).model_copy(
            update={
                "next_action": NextAction(
                    type="open_ui",
                    agent_id="agent-1",
                    route="/workspace/continue",
                    params={"tab": "continue"},
                    metadata={"handling_kind": "ui_handoff"},
                )
            }
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

    async def get_turn(self, *, turn_id, tenant_id, user_id):
        if (
            turn_id == self.turn.turn_id
            and tenant_id == self.turn.tenant_id
            and user_id == self.turn.user_id
        ):
            return self.turn
        return None

    async def attach_activity(self, **kwargs):
        return self.turn


class DelegatedPort:
    def __init__(self) -> None:
        self.started = None
        self.completed = None
        self.failed = None
        self.completed_events = set()
        self.failed_events = set()

    async def start(self, command):
        self.started = command
        return DelegatedRunCommandResult(
            run=DelegatedRunReference(
                run_id="run-1",
                turn_id=command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
                agent_id=command.agent_id,
                plan_id=command.plan_id,
                step_id=command.step_id,
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

    async def fail(self, command):
        duplicate = command.event_id in self.failed_events
        self.failed_events.add(command.event_id)
        self.failed = command
        return DelegatedRunCommandResult(
            run=DelegatedRunReference(
                run_id=command.run_id,
                turn_id=command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
                agent_id=command.agent_id,
                status=DelegatedRunStatus.FAILED,
                state_version=command.expected_state_version + 1,
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            ),
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


class RegistryPort:
    def __init__(self) -> None:
        self.definitions = [
            AgentDefinition(
                agent_id="agent-1",
                name="Agent 1",
                description="Test Agent",
                type="provider_platform",
                invocation=InvocationSpec(type="provider_platform"),
            )
        ]
        self.available_users = []

    async def available_definitions(self, user):
        self.available_users.append(user)
        return list(self.definitions)


class PlanPort:
    def __init__(self) -> None:
        self.confirm_request_id = None
        self.accepted_confirm_request_id = None
        self.plan = Plan(
            plan_id="plan-1",
            tenant_id="oac",
            user_id="trusted-user",
            session_id="session-1",
            steps=[PlanStep(step_id="step-1", agent_id="agent-1", description="do it")],
        )

    async def get_plan(self, plan_id, *, tenant_id, user_id):
        return self.plan if (tenant_id, user_id) == ("oac", "trusted-user") else None

    async def get_active_plan(self, session_id, *, tenant_id, user_id):
        if (
            session_id == self.plan.session_id
            and (tenant_id, user_id) == ("oac", "trusted-user")
            and self.plan.status in {"pending", "running", "blocked"}
        ):
            return self.plan
        return None

    async def confirm(
        self,
        plan_id,
        *,
        tenant_id,
        user_id,
        request_id=None,
        expected_state_version=None,
        publish=True,
    ):
        del publish
        self.confirm_request_id = request_id
        transitioned = request_id == self.accepted_confirm_request_id or (
            self.plan.state_version == expected_state_version and self.plan.status == "pending"
        )
        if transitioned:
            if self.plan.status == "pending":
                self.accepted_confirm_request_id = request_id
                self.plan = self.plan.model_copy(
                    update={"status": "running", "state_version": self.plan.state_version + 1}
                )
        return PlanActionResponse(
            plan_id=plan_id,
            status=self.plan.status,
            current_step_id=self.plan.current_step_id,
            state_version=self.plan.state_version,
            transitioned=transitioned,
        )


def _client(*, user_id: str = "trusted-user", action: str = "open_agent"):
    delegated = DelegatedPort()
    events = EventPort()
    ports = OacAdapterApplicationPorts(
        routing=RoutingPort(action),
        registry=RegistryPort(),
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


def test_route_projects_primary_error_without_external_fallback() -> None:
    client, _, _ = _client()
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: replace(
        ports,
        routing=LLMFailingRoutingPort(),
    )
    response = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-provider-failure",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "我对客户的文案喜欢温柔一点",
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_error"


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
    assert route.headers["X-OIR-Trace-Turn-ID"] == "turn-1"
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


def test_route_ui_handoff_does_not_start_delegated_run_or_issue_ticket() -> None:
    client, delegated, _ = _client()
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: replace(
        ports,
        routing=UiHandoffRoutingPort(),
    )

    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-ui-handoff",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "open the workspace",
        },
    )

    assert route.status_code == 200
    assert route.json()["next_action"] == {
        "type": "open_ui",
        "message": "",
        "agent_id": "agent-1",
        "plan_id": None,
        "step_id": None,
        "route": "/workspace/continue",
        "params": {"tab": "continue"},
        "metadata": {"handling_kind": "ui_handoff"},
    }
    assert route.json()["execution_ticket"] is None
    assert delegated.started is None


def test_agent_error_terminates_the_delegated_run_and_projects_a_redacted_trace() -> None:
    client, delegated, _ = _client()
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: replace(
        ports,
        execution_traces=trace_service,
    )

    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-failure",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )
    ticket = route.json()["execution_ticket"]

    callback = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "event-failure",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "status": "failed",
            "event_type": "agent_error",
            "message": "provider raw error must never reach the trace",
            "output": {
                "error_code": "workflow_execution_failed",
                "provider_payload": "secret upstream detail",
            },
            "execution_ticket": ticket,
        },
    )

    assert callback.status_code == 200
    assert delegated.failed.error == {"code": "workflow_execution_failed"}
    snapshot = asyncio.run(
        trace_service.snapshot(
            ExecutionTraceQuery(
                tenant_id="oac",
                user_id="trusted-user",
                session_id="session-1",
                turn_id="turn-1",
            )
        )
    )
    assert [event.event_type for event in snapshot.events][-2:] == ["agent_event", "agent_result"]
    assert snapshot.events[-1].facts == {
        "agent_id": "agent-1",
        "error_code": "workflow_execution_failed",
    }
    assert "provider raw error" not in snapshot.model_dump_json()
    assert "secret upstream detail" not in snapshot.model_dump_json()


def test_stale_plan_step_completion_returns_canonical_plan_without_claiming_a_ticket() -> None:
    client, delegated, _ = _client()
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    ports.plans.plan = Plan(
        plan_id="plan-1",
        tenant_id="oac",
        user_id="trusted-user",
        session_id="session-1",
        status="blocked",
        current_step_id="step-2",
        state_version=4,
        next_action=NextAction(
            type="open_ui",
            plan_id="plan-1",
            step_id="step-2",
            route="/production",
        ),
        steps=[
            PlanStep(
                step_id="step-1",
                agent_id="agent-1",
                status="completed",
                description="first",
            ),
            PlanStep(
                step_id="step-2",
                agent_id="agent-2",
                status="blocked",
                description="second",
            ),
        ],
    )

    response = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "plan_step_complete_plan-1_step-1_v3",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "plan_id": "plan-1",
            "step_id": "step-1",
            "expected_state_version": 3,
            "status": "completed",
            "event_type": "agent_result",
        },
    )

    assert response.status_code == 200, response.json()
    assert response.json()["conflict"] is True
    assert response.json()["duplicate"] is True
    assert response.json()["route_required"] is False
    assert response.json()["plan"]["current_step"] == "step-2"
    assert response.json()["plan"]["state_version"] == 4
    assert delegated.completed is None


def test_current_plan_step_completion_without_restored_ticket_uses_unique_mapping() -> None:
    client, delegated, _ = _client()
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    ports.plans.plan = ports.plans.plan.model_copy(update={"status": "blocked", "state_version": 3})
    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "plan-step-route",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "plan_control",
            "plan_id": "plan-1",
            "step_id": "step-1",
            "plan_action": "continue",
        },
    )
    assert route.status_code == 200

    response = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "plan_step_complete_plan-1_step-1_v3",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "plan_id": "plan-1",
            "step_id": "step-1",
            "expected_state_version": 3,
            "status": "completed",
            "event_type": "agent_result",
        },
    )

    assert response.status_code == 200, response.json()
    assert response.json()["duplicate"] is False
    assert "conflict" not in response.json()
    assert delegated.completed is not None


def test_plan_step_completion_converges_when_plan_advances_after_precondition_check() -> None:
    client, _, _ = _client()
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    ports.plans.plan = ports.plans.plan.model_copy(update={"status": "blocked", "state_version": 3})

    class CompetingDelegatedPort(DelegatedPort):
        async def complete(self, command):
            del command
            ports.plans.plan = Plan(
                plan_id="plan-1",
                tenant_id="oac",
                user_id="trusted-user",
                session_id="session-1",
                status="completed",
                state_version=4,
                steps=[
                    PlanStep(
                        step_id="step-1",
                        agent_id="agent-1",
                        status="completed",
                        description="done",
                    )
                ],
            )
            raise ValueError("Plan version conflict")

    competing = CompetingDelegatedPort()
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: replace(
        ports,
        delegated_runs=competing,
    )
    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "competing-plan-step-route",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "plan_control",
            "plan_id": "plan-1",
            "step_id": "step-1",
        },
    )
    response = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "plan_step_complete_plan-1_step-1_v3",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "plan_id": "plan-1",
            "step_id": "step-1",
            "expected_state_version": 3,
            "status": "completed",
            "event_type": "agent_result",
            "execution_ticket": route.json()["execution_ticket"],
        },
    )

    assert response.status_code == 200, response.json()
    assert response.json()["conflict"] is True
    assert response.json()["plan"]["status"] == "completed"
    assert response.json()["plan"]["state_version"] == 4


def test_agent_error_event_uses_failure_state_machine_even_with_a_nonterminal_status() -> None:
    client, delegated, _ = _client()

    route = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-error-event",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )
    ticket = route.json()["execution_ticket"]

    callback = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "event-error-type",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "status": "running",
            "event_type": "agent_error",
            "execution_ticket": ticket,
        },
    )

    assert callback.status_code == 200
    assert delegated.failed is not None
    assert delegated.failed.error == {"code": "provider_execution_failed"}


def test_central_route_and_agent_callback_project_one_execution_trace() -> None:
    client, _, _ = _client()
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: replace(
        ports,
        execution_traces=trace_service,
    )

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

    callback = client.post(
        "/api/v1/central/events/agent",
        json={
            "event_id": "event-1",
            "session_id": "session-1",
            "agent_id": "agent-1",
            "status": "completed",
            "event_type": "agent_result",
            "message": "provider result must not be stored as raw trace payload",
            "execution_ticket": ticket,
        },
    )
    assert callback.status_code == 200

    snapshot = asyncio.run(
        trace_service.snapshot(
            ExecutionTraceQuery(
                tenant_id="oac",
                user_id="trusted-user",
                session_id="session-1",
                turn_id="turn-1",
            )
        )
    )

    assert [event.event_type for event in snapshot.events] == [
        "canonical_turn",
        "route_decision",
        "agent_run",
        "agent_event",
        "agent_result",
    ]
    assert "provider result must not" not in snapshot.model_dump_json()


def test_central_route_projects_bounded_context_and_memory_recall_facts() -> None:
    client, _, _ = _client()
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: replace(
        ports,
        routing=ContextRoutingPort(),
        execution_traces=trace_service,
    )

    response = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-context-trace",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )

    assert response.status_code == 200
    snapshot = asyncio.run(
        trace_service.snapshot(
            ExecutionTraceQuery(
                tenant_id="oac",
                user_id="trusted-user",
                session_id="session-1",
                turn_id="turn-1",
            )
        )
    )
    by_type = {event.event_type: event for event in snapshot.events}
    assert by_type["context_pack"].facts == {
        "consumer": "router",
        "included_count": 3,
        "excluded_count": 2,
        "degraded": True,
        "reason_code": "context_timeout",
    }
    assert by_type["memory_recall"].facts == {
        "scope": "user_preference",
        "used_count": 1,
        "excluded_count": 1,
        "degraded": True,
        "reason_code": "context_timeout",
    }
    assert "must never reach the trace" not in snapshot.model_dump_json()


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
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
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

    confirm = client.post(
        "/api/v1/central/plans/plan-1/confirm",
        json={"request_id": "confirm-1", "expected_state_version": 0},
    )
    assert confirm.status_code == 200
    assert ports.plans.confirm_request_id == "confirm-1"
    assert confirm.json()["current_step"]["runtime_status"] == "running"
    duplicate = client.post(
        "/api/v1/central/plans/plan-1/confirm",
        json={"request_id": "confirm-1", "expected_state_version": 0},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == confirm.json()["status"] == "running"
    assert duplicate.json()["state_version"] == confirm.json()["state_version"]
    assert duplicate.json()["conflict"] is False
    stale = client.post(
        "/api/v1/central/plans/plan-1/confirm",
        json={"request_id": "confirm-2", "expected_state_version": 0},
    )
    assert stale.status_code == 200
    assert stale.json()["conflict"] is True

    active = client.get("/api/v1/central/active-plan?session_id=session-1")
    assert active.status_code == 200
    assert active.json()["plan"]["status"] == "running"
    assert active.json()["plan"]["state_version"] == confirm.json()["state_version"]


def test_plan_confirm_rejects_an_unavailable_agent_before_state_change() -> None:
    client, _, _ = _client()
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    ports.registry.definitions = []

    response = client.post(
        "/api/v1/central/plans/plan-1/confirm",
        json={"request_id": "confirm-1", "expected_state_version": 0},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "agent_not_available"
    assert ports.plans.confirm_request_id is None
    assert ports.plans.plan.status == "pending"
    assert ports.plans.plan.state_version == 0
    assert len(ports.registry.available_users) == 1
    assert ports.registry.available_users[0].id == "trusted-user"
    assert ports.registry.available_users[0].tenant_id == "oac"


def test_active_plan_does_not_disclose_another_users_plan() -> None:
    client, _, _ = _client(user_id="other-user")

    active = client.get("/api/v1/central/active-plan?session_id=session-1")

    assert active.status_code == 200
    assert active.json() == {"plan": None}


def test_route_failure_is_fail_closed_without_irs_runtime_dependency() -> None:
    client, _, _ = _client(action="reply")
    ports = client.app.dependency_overrides[get_oac_adapter_application_ports]()
    ports = replace(ports, routing=FailingRoutingPort())
    client.app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports

    response = client.post(
        "/api/v1/central/route",
        json={
            "request_id": "request-fail-closed",
            "session_id": "session-1",
            "user_id": "trusted-user",
            "user_tags": ["运营版"],
            "source": "central_chat",
            "user_query": "hello",
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "internal_error",
        "message": "Internal server error",
        "details": {},
    }


class FailingRoutingPort:
    async def route(self, _request):
        raise TimeoutError("route timeout")

    other_client, _, _ = _client(user_id="other-user")
    rejected = other_client.post(
        "/api/v1/central/plans/plan-1/confirm",
        json={"request_id": "confirm-other", "expected_state_version": 0},
    )
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
    if action == "show_plan":
        assert response.json()["plan"]["state_version"] == 4
        assert response.json()["next_action"]["type"] == "confirm_plan"


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
    assert routing.last_request.user.attributes == {
        "tenant_id": "oac",
        "knowledge_access_tags": ["运营版"],
    }

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
