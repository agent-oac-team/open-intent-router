import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_delegated_run_service,
    get_event_service,
    get_execution_ticket_service,
    get_native_agent_event_service,
    get_run_repository,
)
from app.main import create_app
from app.repositories.delegated_runs import (
    MemoryDelegatedRunCancelStore,
    MemoryDelegatedRunCompletionStore,
    MemoryDelegatedRunFailureStore,
    MemoryDelegatedRunProgressStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.execution_tickets import MemoryExecutionTicketStore
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import MemoryTurnRepository
from app.schemas.delegated_runs import (
    DelegatedRunCompleteCommand,
    DelegatedRunProgressCommand,
    DelegatedRunReference,
    DelegatedRunStartCommand,
)
from app.schemas.events import AgentEvent
from app.schemas.execution_tickets import ExecutionTicketStatus
from app.schemas.plans import Plan, PlanStep
from app.schemas.turns import TurnUserInput
from app.services.agent_event_service import AgentEventRejected, NativeAgentEventService
from app.services.delegated_run_service import DelegatedRunService
from app.services.event_service import EventService
from app.services.execution_ticket_service import ExecutionTicketService
from app.services.turn_service import TurnService


@dataclass
class NativeEventRuntime:
    client: TestClient
    run_id: str
    turn_id: str
    ticket: str
    run_ref: DelegatedRunReference
    delegated: DelegatedRunService
    tickets: ExecutionTicketService
    runs: MemoryRunRepository
    events: MemoryEventRepository
    results: MemoryResultRepository
    plans: MemoryPlanRepository
    turns: MemoryTurnRepository
    outbox: MemoryTurnOutboxRepository

    def event(self, *, event_id: str = "event-1", **updates):
        payload = {
            "event_id": event_id,
            "session_id": "session-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
            "turn_id": self.turn_id,
            "event_type": "agent_progress",
            "status": "running",
            "sequence": 1,
            "run_state_version": 1,
            "plan_id": "plan-1",
            "step_id": "step-1",
            "payload": {"progress": 20},
        }
        payload.update(updates)
        return payload


def test_native_event_http_module_is_adapter_only() -> None:
    source = Path("app/api/events.py").read_text(encoding="utf-8")

    for business_operation in (
        "tickets.claim(",
        "tickets.consume(",
        "tickets.release_claim(",
        "delegated_runs.complete(",
        "delegated_runs.fail(",
        "delegated_runs.cancel(",
        "_validate_ticket_correlation",
        "_validate_canonical_run",
    ):
        assert business_operation not in source


@pytest.fixture
async def native_event_runtime() -> NativeEventRuntime:
    runs = MemoryRunRepository()
    events = MemoryEventRepository()
    results = MemoryResultRepository()
    plans = MemoryPlanRepository()
    turns = MemoryTurnRepository()
    outbox = MemoryTurnOutboxRepository()
    await plans.save(
        Plan(
            plan_id="plan-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            status="running",
            current_step_id="step-1",
            state_version=1,
            steps=[
                PlanStep(
                    step_id="step-1",
                    agent_id="agent-1",
                    description="delegate",
                    status="running",
                )
            ],
        )
    )
    delegated = DelegatedRunService(
        MemoryDelegatedRunStartStore(
            run_repository=runs,
            turn_repository=turns,
            plan_repository=plans,
        ),
        progress_store=MemoryDelegatedRunProgressStore(
            run_repository=runs,
            event_repository=events,
            plan_repository=plans,
        ),
        completion_store=MemoryDelegatedRunCompletionStore(
            run_repository=runs,
            result_repository=results,
            event_repository=events,
            turn_repository=turns,
            outbox_repository=outbox,
            plan_repository=plans,
        ),
        failure_store=MemoryDelegatedRunFailureStore(
            run_repository=runs,
            event_repository=events,
            turn_repository=turns,
            outbox_repository=outbox,
            plan_repository=plans,
        ),
        cancel_store=MemoryDelegatedRunCancelStore(
            run_repository=runs,
            event_repository=events,
            turn_repository=turns,
            outbox_repository=outbox,
            plan_repository=plans,
        ),
    )
    turn = await TurnService(turns).start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host",
        user_input=TurnUserInput(text="delegate"),
    )
    started = await delegated.start(
        DelegatedRunStartCommand(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            request_id="request-1",
            turn_id=turn.turn.turn_id,
            agent_id="agent-1",
            plan_id="plan-1",
            step_id="step-1",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    tickets = ExecutionTicketService(MemoryExecutionTicketStore(), secret="ticket-secret")
    issued = await tickets.issue(
        started.run,
        request_id="request-1",
        purpose="agent_event",
        ttl_seconds=60,
    )
    app = create_app()
    app.dependency_overrides[get_delegated_run_service] = lambda: delegated
    app.dependency_overrides[get_execution_ticket_service] = lambda: tickets
    app.dependency_overrides[get_run_repository] = lambda: runs
    app.dependency_overrides[get_event_service] = lambda: EventService(events)
    app.dependency_overrides[get_native_agent_event_service] = lambda: NativeAgentEventService(
        tickets=tickets,
        delegated_runs=delegated,
        run_repository=runs,
        ticket_lease_seconds=30,
    )
    return NativeEventRuntime(
        client=TestClient(app),
        run_id=started.run.run_id,
        turn_id=started.run.turn_id,
        ticket=issued.ticket,
        run_ref=started.run,
        delegated=delegated,
        tickets=tickets,
        runs=runs,
        events=events,
        results=results,
        plans=plans,
        turns=turns,
        outbox=outbox,
    )


async def test_native_event_requires_execution_ticket_without_partial_writes(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime

    response = runtime.client.post(
        f"/api/v1/runs/{runtime.run_id}/events",
        json=runtime.event(),
    )

    run = await runtime.runs.get_run(runtime.run_id)
    turn = await runtime.turns.get(
        runtime.turn_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    plan = await runtime.plans.get("plan-1", tenant_id="tenant-1", user_id="user-1")
    assert response.status_code == 401
    assert run and run.state_version == 1 and run.status == "pending"
    assert turn and turn.status == "running" and turn.state_version == 2
    assert runtime.events.agent_events == {}
    assert runtime.results.results == []
    assert plan and plan.status == "running" and plan.state_version == 1
    assert runtime.outbox.events == {}


async def test_matching_ticket_releases_after_progress_and_replay_is_idempotent(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime
    headers = {"X-OIR-Execution-Ticket": runtime.ticket}

    first = runtime.client.post(
        "/api/v1/events/agent",
        headers=headers,
        json=runtime.event(),
    )
    duplicate = runtime.client.post(
        "/api/v1/events/agent",
        headers=headers,
        json=runtime.event(),
    )

    run = await runtime.runs.get_run(runtime.run_id)
    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    event = await runtime.events.get_event(
        "event-1",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    assert run and run.status == "running" and run.state_version == 2
    assert event and event.run_id == runtime.run_id and event.turn_id == runtime.turn_id
    assert ticket.status == ExecutionTicketStatus.ISSUED
    assert ticket.run_state_version == 2 and ticket.event_sequence == 1
    assert ticket.lease_token is None


async def test_agent_clarify_blocks_the_ticket_bound_plan_step(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime

    response = runtime.client.post(
        "/api/v1/events/agent",
        headers={"X-OIR-Execution-Ticket": runtime.ticket},
        json=runtime.event(
            event_id="event-clarify",
            event_type="agent_clarify",
            status="blocked",
            payload={"question": "Which account?"},
        ),
    )

    plan = await runtime.plans.get("plan-1", tenant_id="tenant-1", user_id="user-1")
    event = await runtime.events.get_event(
        "event-clarify",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert response.status_code == 200
    assert plan is not None and plan.status == "blocked"
    assert plan.current_step_id == "step-1"
    assert plan.steps[0].status == "blocked"
    assert event is not None and event.event_type == "agent_clarify"
    assert ticket.status == ExecutionTicketStatus.ISSUED
    assert ticket.run_state_version == 2 and ticket.event_sequence == 1


async def test_committed_progress_duplicate_advances_stale_ticket_cursor(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime
    occurred_at = datetime.now(UTC)
    await runtime.delegated.progress(
        DelegatedRunProgressCommand(
            event_id="event-1",
            run_id=runtime.run_id,
            turn_id=runtime.turn_id,
            tenant_id="tenant-1",
            user_id="user-1",
            agent_id="agent-1",
            plan_id="plan-1",
            step_id="step-1",
            expected_state_version=1,
            sequence=1,
            status="running",
            payload={"progress": 20},
            occurred_at=occurred_at,
        )
    )

    response = runtime.client.post(
        "/api/v1/events/agent",
        headers={"X-OIR-Execution-Ticket": runtime.ticket},
        json=runtime.event(created_at=occurred_at.isoformat()),
    )

    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert ticket.status == ExecutionTicketStatus.ISSUED
    assert ticket.run_state_version == 2
    assert ticket.event_sequence == 1
    assert ticket.lease_token is None


async def test_matching_ticket_is_consumed_after_atomic_terminal_event(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime
    payload = runtime.event(
        event_id="event-final",
        event_type="agent_result",
        status="completed",
        payload={
            "result_id": "result-final",
            "message": "done",
            "output": {"answer": "done"},
        },
    )
    headers = {"X-OIR-Execution-Ticket": runtime.ticket}

    first = runtime.client.post(
        f"/api/v1/runs/{runtime.run_id}/events",
        headers=headers,
        json=payload,
    )
    duplicate = runtime.client.post(
        f"/api/v1/runs/{runtime.run_id}/events",
        headers=headers,
        json=payload,
    )

    run = await runtime.runs.get_run(runtime.run_id)
    turn = await runtime.turns.get(
        runtime.turn_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    plan = await runtime.plans.get("plan-1", tenant_id="tenant-1", user_id="user-1")
    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    assert run and run.status == "completed" and run.terminal_event_id == "event-final"
    assert turn and turn.status == "completed" and turn.references.result_ids == ["result-final"]
    assert plan and plan.status == "completed" and plan.steps[0].status == "completed"
    assert [item.result_id for item in runtime.results.results] == ["result-final"]
    assert len(runtime.events.agent_events) == 1
    assert len(runtime.outbox.events) == 1
    assert ticket.status == ExecutionTicketStatus.CONSUMED
    assert ticket.consumed_event_id == "event-final"


@pytest.mark.parametrize(
    "mutated",
    [
        {
            "event_type": "agent_error",
            "status": "failed",
            "payload": {"error": {"code": "changed"}},
        },
        {
            "event_type": "agent_result",
            "status": "completed",
            "payload": {
                "result_id": "different-result",
                "message": "changed",
                "output": {"answer": "changed"},
            },
        },
        {
            "event_type": "agent_result",
            "status": "completed",
            "payload": {
                "result_id": "result-final",
                "message": "changed",
                "output": {"answer": "changed"},
            },
        },
        {
            "event_type": "agent_result",
            "status": "completed",
            "sequence": 2,
            "payload": {
                "result_id": "result-final",
                "message": "done",
                "output": {"answer": "done"},
            },
        },
        {
            "event_type": "agent_result",
            "status": "completed",
            "created_at": "2099-01-01T00:00:00Z",
            "payload": {
                "result_id": "result-final",
                "message": "done",
                "output": {"answer": "done"},
            },
        },
    ],
)
async def test_consumed_ticket_rejects_non_identical_terminal_replay(
    native_event_runtime: NativeEventRuntime,
    mutated: dict,
) -> None:
    runtime = native_event_runtime
    original = runtime.event(
        event_id="event-final",
        event_type="agent_result",
        status="completed",
        payload={
            "result_id": "result-final",
            "message": "done",
            "output": {"answer": "done"},
        },
    )
    headers = {"X-OIR-Execution-Ticket": runtime.ticket}
    first = runtime.client.post("/api/v1/events/agent", headers=headers, json=original)

    replay = runtime.client.post(
        "/api/v1/events/agent",
        headers=headers,
        json=runtime.event(event_id="event-final", **mutated),
    )

    run = await runtime.runs.get_run(runtime.run_id)
    event = await runtime.events.get_event(
        "event-final",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert first.status_code == 200
    assert replay.status_code == 409
    assert replay.json()["detail"] == "agent_event_conflict"
    assert run and run.status == "completed" and run.output == {"answer": "done"}
    assert event and event.event_type == "agent_result"
    assert event.payload == {"result_id": "result-final"}
    assert [item.result_id for item in runtime.results.results] == ["result-final"]
    assert ticket.status == ExecutionTicketStatus.CONSUMED
    assert ticket.consumed_event_id == "event-final"


async def test_terminal_replay_without_created_at_reuses_persisted_occurrence_time(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime

    class CapturingCompletionService:
        def __init__(self, delegate: DelegatedRunService) -> None:
            self.delegate = delegate
            self.occurred_at: list[datetime] = []

        async def complete(self, command):
            self.occurred_at.append(command.occurred_at)
            return await self.delegate.complete(command)

    capturing = CapturingCompletionService(runtime.delegated)
    service = NativeAgentEventService(
        tickets=runtime.tickets,
        delegated_runs=capturing,
        run_repository=runtime.runs,
        ticket_lease_seconds=30,
    )
    payload = AgentEvent.model_validate(
        runtime.event(
            event_id="event-final",
            event_type="agent_result",
            status="completed",
            payload={
                "result_id": "result-final",
                "message": "done",
                "output": {"answer": "done"},
            },
        )
    )

    first = await service.record(payload, execution_ticket=runtime.ticket)
    await asyncio.sleep(0.01)
    duplicate = await service.record(payload, execution_ticket=runtime.ticket)

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert len(capturing.occurred_at) == 2
    assert capturing.occurred_at[1] == capturing.occurred_at[0]


async def test_committed_terminal_duplicate_converges_ticket_to_consumed(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime
    occurred_at = datetime.now(UTC)
    await runtime.delegated.complete(
        DelegatedRunCompleteCommand(
            event_id="event-final",
            run_id=runtime.run_id,
            turn_id=runtime.turn_id,
            tenant_id="tenant-1",
            user_id="user-1",
            agent_id="agent-1",
            plan_id="plan-1",
            step_id="step-1",
            expected_state_version=1,
            occurred_at=occurred_at,
            result_id="result-final",
            response_text="done",
            output={"answer": "done"},
        )
    )

    response = runtime.client.post(
        "/api/v1/events/agent",
        headers={"X-OIR-Execution-Ticket": runtime.ticket},
        json=runtime.event(
            event_id="event-final",
            event_type="agent_result",
            status="completed",
            created_at=occurred_at.isoformat(),
            payload={
                "result_id": "result-final",
                "message": "done",
                "output": {"answer": "done"},
            },
        ),
    )

    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert ticket.status == ExecutionTicketStatus.CONSUMED
    assert ticket.consumed_event_id == "event-final"


async def test_concurrent_same_id_terminal_callbacks_keep_claim_exclusive_and_converge(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime

    class GatedCompletionService:
        def __init__(self, delegate: DelegatedRunService) -> None:
            self.delegate = delegate
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, command):
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=1)
            return await self.delegate.complete(command)

    gated = GatedCompletionService(runtime.delegated)
    service = NativeAgentEventService(
        tickets=runtime.tickets,
        delegated_runs=gated,
        run_repository=runtime.runs,
        ticket_lease_seconds=30,
    )
    payload = AgentEvent.model_validate(
        runtime.event(
            event_id="event-final",
            event_type="agent_result",
            status="completed",
            payload={
                "result_id": "result-final",
                "message": "done",
                "output": {"answer": "done"},
            },
        )
    )

    first_task = asyncio.create_task(service.record(payload, execution_ticket=runtime.ticket))
    await asyncio.wait_for(gated.entered.wait(), timeout=1)
    second_task = asyncio.create_task(service.record(payload, execution_ticket=runtime.ticket))
    done_before_release, _pending = await asyncio.wait({second_task}, timeout=0.1)
    gated.release.set()
    first, second = await asyncio.gather(first_task, second_task, return_exceptions=True)

    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert second_task in done_before_release
    assert not isinstance(first, Exception) and first.duplicate is False
    assert isinstance(second, AgentEventRejected)
    assert second.category == "conflict"
    assert second.detail == "execution_ticket_unavailable"
    assert ticket.status == ExecutionTicketStatus.CONSUMED
    assert ticket.consumed_event_id == "event-final"
    assert len(runtime.events.agent_events) == 1
    assert [item.result_id for item in runtime.results.results] == ["result-final"]


@pytest.mark.parametrize("offset", [timedelta(days=-30), timedelta(days=30)])
async def test_terminal_event_created_at_does_not_control_ticket_lifecycle_clock(
    native_event_runtime: NativeEventRuntime,
    offset: timedelta,
) -> None:
    runtime = native_event_runtime
    request_started_at = datetime.now(UTC)
    response = runtime.client.post(
        "/api/v1/events/agent",
        headers={"X-OIR-Execution-Ticket": runtime.ticket},
        json=runtime.event(
            event_id="event-final",
            event_type="agent_result",
            status="completed",
            created_at=(request_started_at + offset).isoformat(),
            payload={
                "result_id": "result-final",
                "message": "done",
                "output": {"answer": "done"},
            },
        ),
    )
    request_finished_at = datetime.now(UTC)

    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert response.status_code == 200
    assert ticket.status == ExecutionTicketStatus.CONSUMED
    assert ticket.consumed_at is not None
    assert request_started_at <= ticket.consumed_at <= request_finished_at


async def test_matching_ticket_consumes_atomic_cancellation_confirmation(
    native_event_runtime: NativeEventRuntime,
) -> None:
    runtime = native_event_runtime

    response = runtime.client.post(
        f"/api/v1/runs/{runtime.run_id}/events",
        headers={"X-OIR-Execution-Ticket": runtime.ticket},
        json=runtime.event(
            event_id="event-cancelled",
            event_type="agent_cancelled",
            status="cancelled",
            payload={"reason": "user_requested"},
        ),
    )

    run = await runtime.runs.get_run(runtime.run_id)
    turn = await runtime.turns.get(
        runtime.turn_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    ticket = await runtime.tickets.resolve_bound(runtime.ticket, purpose="agent_event")
    assert response.status_code == 200
    assert run and run.status == "cancelled"
    assert turn and turn.status == "cancelled"
    assert ticket.status == ExecutionTicketStatus.CONSUMED


@pytest.mark.parametrize("ticket_kind", ["invalid_signature", "expired", "wrong_purpose"])
async def test_invalid_ticket_is_rejected_without_partial_writes(
    native_event_runtime: NativeEventRuntime,
    ticket_kind: str,
) -> None:
    runtime = native_event_runtime
    if ticket_kind == "invalid_signature":
        ticket = f"{runtime.ticket}x"
    elif ticket_kind == "expired":
        issued = await runtime.tickets.issue(
            runtime.run_ref,
            request_id="request-1",
            purpose="agent_event",
            ttl_seconds=1,
            now=datetime.now(UTC) - timedelta(seconds=2),
        )
        ticket = issued.ticket
    else:
        issued = await runtime.tickets.issue(
            runtime.run_ref,
            request_id="request-1",
            purpose="plan_control",
            ttl_seconds=60,
        )
        ticket = issued.ticket

    response = runtime.client.post(
        f"/api/v1/runs/{runtime.run_id}/events",
        headers={"X-OIR-Execution-Ticket": ticket},
        json=runtime.event(),
    )

    assert response.status_code == 401
    await _assert_pristine(runtime)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "other-run"),
        ("turn_id", "other-turn"),
        ("user_id", "other-user"),
        ("tenant_id", "other-tenant"),
        ("agent_id", "other-agent"),
        ("plan_id", "other-plan"),
        ("step_id", "other-step"),
        ("session_id", "other-session"),
    ],
)
async def test_ticket_correlation_conflict_has_no_partial_writes(
    native_event_runtime: NativeEventRuntime,
    field: str,
    value: str,
) -> None:
    runtime = native_event_runtime

    response = runtime.client.post(
        f"/api/v1/runs/{runtime.run_id}/events",
        headers={"X-OIR-Execution-Ticket": runtime.ticket},
        json=runtime.event(**{field: value}),
    )

    assert response.status_code == 409
    await _assert_pristine(runtime)


async def _assert_pristine(runtime: NativeEventRuntime) -> None:
    run = await runtime.runs.get_run(runtime.run_id)
    turn = await runtime.turns.get(
        runtime.turn_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    plan = await runtime.plans.get("plan-1", tenant_id="tenant-1", user_id="user-1")
    assert run and run.state_version == 1 and run.status == "pending"
    assert turn and turn.status == "running" and turn.state_version == 2
    assert plan and plan.status == "running" and plan.state_version == 1
    assert runtime.events.agent_events == {}
    assert runtime.results.results == []
    assert runtime.outbox.events == {}
