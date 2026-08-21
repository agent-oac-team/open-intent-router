import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import ExternalExecutionBindingUnavailableError
from app.repositories.delegated_runs import (
    MemoryDelegatedRunCancelStore,
    MemoryDelegatedRunCompletionStore,
    MemoryDelegatedRunFailureStore,
    MemoryDelegatedRunProgressStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.execution_tickets import MemoryExecutionTicketStore, ticket_hash
from app.repositories.external_execution_acceptances import (
    DatabaseExternalExecutionAcceptanceStore,
    ExternalExecutionAcceptanceConflict,
    MemoryExternalExecutionAcceptanceStore,
)
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunCompleteCommand, DelegatedRunProgressCommand
from app.schemas.execution_tickets import LegacyExecutionCorrelationQuery
from app.schemas.execution_traces import ExecutionTraceEventDraft
from app.schemas.external_execution import (
    ExternalExecutionAcceptanceReservation,
    ExternalExecutorAcceptance,
    ExternalExecutorAcceptanceRequest,
)
from app.schemas.logs import (
    ExternalExecutionBindingSnapshot,
    external_execution_binding_fingerprint,
)
from app.schemas.plans import Plan, PlanStep
from app.schemas.routing import (
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.execution_ticket_service import ExecutionTicketService
from app.services.external_execution_service import ExternalExecutionService
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime
from app.services.router_service import RouterService
from app.services.turn_service import TurnService
from host_adapters.oac.external_executor import OacExternalExecutor


class _NoRegistryReads:
    async def available_definitions(self, _user):
        raise AssertionError("Route must use the trusted Registry Snapshot")


class _ExternalTargetLLM:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request.request_id or "external-request",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                action="open_agent",
                target_agent_id="external-agent",
                confidence=0.9,
                reason="External work is required.",
            ),
            context=RouteContext(candidate_agent_ids=["external-agent"]),
        )


class _Executor:
    def __init__(
        self,
        *,
        supported: bool = True,
        acceptance: ExternalExecutorAcceptance | None = None,
    ) -> None:
        self.supported = supported
        self.acceptance = acceptance or ExternalExecutorAcceptance(
            accepted=True,
            binding_id="external_binding",
        )
        self.requests: list[ExternalExecutorAcceptanceRequest] = []

    def supports(self, _executor_ref: str) -> bool:
        return self.supported

    async def accept(
        self,
        request: ExternalExecutorAcceptanceRequest,
    ) -> ExternalExecutorAcceptance:
        self.requests.append(request)
        return self.acceptance


class _ConcurrentAcceptanceExecutor:
    """Force two independently locked Core services to race the Host boundary."""

    def __init__(self, delegate: OacExternalExecutor) -> None:
        self._delegate = delegate
        self.accept_calls = 0
        self._arrivals = 0
        self._release = asyncio.Event()

    def supports(self, executor_ref: str) -> bool:
        return self._delegate.supports(executor_ref)

    async def accept(
        self,
        request: ExternalExecutorAcceptanceRequest,
    ) -> ExternalExecutorAcceptance:
        self.accept_calls += 1
        self._arrivals += 1
        if self._arrivals == 2:
            self._release.set()
        await self._release.wait()
        return await self._delegate.accept(request)


def _definition(*, tenant_id: str = "tenant-1") -> dict[str, object]:
    return {
        "schema_version": "oir-agent-v2",
        "agent_id": "external-agent",
        "name": "External Agent",
        "description": "Delegates accepted work to the Host.",
        "revision": 7,
        "access_policy": {"allow_roles": ["operator"], "allow_tenants": [tenant_id]},
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "output_schema": {"type": "object", "properties": {}},
        "handling": {
            "kind": "external_execution",
            "executor_ref": "host_executor",
            "params": {"task": "run"},
        },
    }


def _request(
    *,
    request_id: str = "external-request",
    tenant_id: str = "tenant-1",
) -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": request_id,
            "session_id": "external-session",
            "user": {
                "id": "external-user",
                "roles": ["operator"],
                "attributes": {"tenant_id": tenant_id},
            },
            "input": {"text": "delegate this work"},
        }
    )


async def _routed_external_response(
    executor: _Executor,
    *,
    tenant_id: str = "tenant-1",
) -> tuple[RouteRequest, RouteResponse]:
    snapshot_runtime = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(None, external_executor=executor)
    )
    snapshot_runtime.load([_definition(tenant_id=tenant_id)], source="external-execution-test")
    router = RouterService(
        settings=Settings(storage_backend="memory"),
        registry=_NoRegistryReads(),
        llm_client=_ExternalTargetLLM(),
        snapshot_runtime=snapshot_runtime,
    )
    request = _request(tenant_id=tenant_id)
    return request, await router.route(request)


async def _service(
    executor: _Executor,
) -> tuple[ExternalExecutionService, MemoryRunRepository, MemoryExecutionTicketStore, TurnService]:
    runs = MemoryRunRepository()
    turns = TurnService(MemoryTurnRepository())
    events = MemoryEventRepository()
    outbox = MemoryTurnOutboxRepository()
    plans = MemoryPlanRepository()
    results = MemoryResultRepository()
    delegated_runs = DelegatedRunService(
        MemoryDelegatedRunStartStore(
            run_repository=runs,
            turn_repository=turns.repository,
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
            turn_repository=turns.repository,
            outbox_repository=outbox,
            plan_repository=plans,
        ),
        failure_store=MemoryDelegatedRunFailureStore(
            run_repository=runs,
            event_repository=events,
            turn_repository=turns.repository,
            outbox_repository=outbox,
            plan_repository=plans,
        ),
        cancel_store=MemoryDelegatedRunCancelStore(
            run_repository=runs,
            event_repository=events,
            turn_repository=turns.repository,
            outbox_repository=outbox,
            plan_repository=plans,
        ),
    )
    ticket_store = MemoryExecutionTicketStore()
    tickets = ExecutionTicketService(ticket_store, secret="external-test-secret")
    return (
        ExternalExecutionService(
            external_executor=executor,
            delegated_runs=delegated_runs,
            tickets=tickets,
            ticket_ttl_seconds=60,
        ),
        runs,
        ticket_store,
        turns,
    )


class _TicketIssuerFailure:
    secret = "external-test-secret"

    async def issue(self, *_args, **_kwargs):
        raise RuntimeError("ticket store unavailable")

    async def recover_after_issue_failure(self, *_args, **_kwargs):
        return None

    async def recover_committed_ticket(self, *_args, **_kwargs):
        return None


class _CommitThenRaiseTicketIssuer:
    """Model an unknown commit after a durable Ticket transaction succeeds."""

    def __init__(self, delegate: ExecutionTicketService) -> None:
        self._delegate = delegate
        self.secret = delegate.secret

    async def issue(self, *args, **kwargs):
        await self._delegate.issue(*args, **kwargs)
        raise RuntimeError("ticket response was lost after commit")

    async def recover_after_issue_failure(self, *args, **kwargs):
        return await self._delegate.recover_after_issue_failure(*args, **kwargs)

    async def recover_committed_ticket(self, *args, **kwargs):
        return await self._delegate.recover_committed_ticket(*args, **kwargs)


class _FirstTicketIssueFails:
    """Let a duplicate Route retry issue while the first handoff fails."""

    def __init__(self, delegate: ExecutionTicketService) -> None:
        self._delegate = delegate
        self.secret = delegate.secret
        self.first_issue_started = asyncio.Event()
        self.release_first_issue = asyncio.Event()
        self._calls = 0

    async def issue(self, *args, **kwargs):
        self._calls += 1
        if self._calls == 1:
            self.first_issue_started.set()
            await self.release_first_issue.wait()
            raise RuntimeError("first Ticket issue failed")
        return await self._delegate.issue(*args, **kwargs)

    async def recover_after_issue_failure(self, *args, **kwargs):
        return await self._delegate.recover_after_issue_failure(*args, **kwargs)

    async def recover_committed_ticket(self, *args, **kwargs):
        return await self._delegate.recover_committed_ticket(*args, **kwargs)


class _CancellationForbiddenDelegatedRuns:
    def __init__(self, delegate: DelegatedRunService) -> None:
        self.delegate = delegate
        self.cancel_calls = 0

    async def start(self, command):
        return await self.delegate.start(command)

    async def find_existing(self, command):
        return await self.delegate.find_existing(command)

    async def cancel(self, _command):
        self.cancel_calls += 1
        raise AssertionError("Ticket issuance failure is not an external cancellation")

    async def fail(self, command):
        return await self.delegate.fail(command)


async def test_route_returns_safe_wait_for_external_execution_without_invocation() -> None:
    request, route = await _routed_external_response(_Executor())

    assert route.invocation is None
    assert route.next_action is not None
    assert route.next_action.type == "wait_for_agent_event"
    assert route.next_action.metadata == {"handling_kind": "external_execution"}
    assert route.has_trusted_external_execution_for(request)
    assert "host_executor" not in route.model_dump_json()


@pytest.mark.asyncio
async def test_accepted_external_execution_persists_bound_run_then_issues_ticket() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )

    started = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    run = await runs.get_run(started.run.run_id)
    assert run.agent_revision == 7
    assert run.handling_kind == "external_execution"
    assert run.binding_snapshot is not None
    assert run.binding_snapshot.model_dump(mode="json") == {
        "schema_version": "oir-binding-v1",
        "kind": "external_execution",
        "executor_ref": "host_executor",
        "executor_binding_id": external_execution_binding_fingerprint(
            "external_binding",
            secret="external-test-secret",
        ),
    }
    assert executor.requests[0].executor_ref == "host_executor"
    assert executor.requests[0].principal.tenant_id == "tenant-1"
    assert executor.requests[0].params == {"task": "run"}
    assert "task" not in run.binding_snapshot.model_dump_json()
    assert "external_binding" not in run.binding_snapshot.model_dump_json()
    assert "host_executor" in started.binding_trace_facts.values()
    assert "task" not in started.binding_trace_facts.values()
    assert "external_binding" not in started.binding_trace_facts.values()
    assert len(ticket_store.records) == 1
    record = next(iter(ticket_store.records.values()))
    assert record.claims.run_id == started.run.run_id
    assert record.claims.turn_id == turn.turn.turn_id
    assert record.claims.agent_id == "external-agent"


@pytest.mark.asyncio
async def test_external_execution_retry_after_service_restart_reuses_canonical_binding() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )

    first = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    executor.acceptance = ExternalExecutorAcceptance(
        accepted=True,
        binding_id="replacement_binding",
    )
    restarted_service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=DelegatedRunService(
            MemoryDelegatedRunStartStore(
                run_repository=runs,
                turn_repository=turns.repository,
            )
        ),
        tickets=service._tickets,
        ticket_ttl_seconds=60,
    )
    retried = await restarted_service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert len(executor.requests) == 1
    assert executor.requests[0].acceptance_id.startswith("external_acceptance_")
    assert retried.run.run_id == first.run.run_id
    assert retried.binding_snapshot == first.binding_snapshot
    assert retried.execution_ticket == first.execution_ticket
    legacy = await service._tickets.claim_legacy(
        LegacyExecutionCorrelationQuery(
            tenant_id="tenant-1",
            user_id="external-user",
            request_id=request.request_id,
            agent_id="external-agent",
            plan_id=None,
            step_id=None,
            event_id="legacy-external-retry-event",
            purpose="agent_event",
            now=datetime.now(UTC),
        ),
        owner="legacy-oac-callback",
        lease_seconds=30,
    )
    assert legacy.record.ticket_hash == ticket_hash(retried.execution_ticket)
    assert (
        len(
            [
                record
                for record in ticket_store.records.values()
                if record.claims.run_id == first.run.run_id
                and record.status.value in {"issued", "claimed"}
            ]
        )
        == 1
    )


async def test_external_execution_retry_reuses_an_inflight_ticket_claim() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    first = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    claimed = await service._tickets.claim(
        first.execution_ticket,
        tenant_id="tenant-1",
        user_id="external-user",
        purpose="agent_event",
        owner="legacy-oac-callback",
        lease_seconds=30,
    )

    retried = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert retried.execution_ticket == first.execution_ticket
    active = await service._tickets.store.get(ticket_hash(first.execution_ticket))
    assert active is not None
    assert active.status.value == "claimed"
    consumed = await service._tickets.consume(
        first.execution_ticket,
        event_id="inflight-callback-terminal-event",
        owner="legacy-oac-callback",
        lease_token=claimed.record.lease_token or "",
    )
    assert consumed.status.value == "consumed"
    assert len(ticket_store.records) == 1
    assert len(runs.runs) == 1


async def test_external_execution_retry_recovers_an_expired_ticket_lease() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, _, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    first = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    stale_claim = await service._tickets.claim(
        first.execution_ticket,
        tenant_id="tenant-1",
        user_id="external-user",
        purpose="agent_event",
        owner="crashed-callback-worker",
        lease_seconds=30,
        now=datetime.now(UTC) - timedelta(minutes=1),
    )

    retried = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert retried.execution_ticket == first.execution_ticket
    recovered = await service._tickets.claim(
        retried.execution_ticket,
        tenant_id="tenant-1",
        user_id="external-user",
        purpose="agent_event",
        owner="recovered-callback-worker",
        lease_seconds=30,
    )
    assert recovered.record.lease_token != stale_claim.record.lease_token
    assert len(ticket_store.records) == 1


async def test_external_execution_retry_resumes_progressed_ticket_cursor() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, _, _, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    first = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    progress_claim = await service._tickets.claim(
        first.execution_ticket,
        tenant_id="tenant-1",
        user_id="external-user",
        purpose="agent_event",
        owner="progress-callback-worker",
        lease_seconds=30,
    )
    progressed = await service._delegated_runs.progress(
        DelegatedRunProgressCommand(
            event_id="event_external_progress",
            run_id=first.run.run_id,
            turn_id=first.run.turn_id,
            tenant_id="tenant-1",
            user_id="external-user",
            agent_id="external-agent",
            expected_state_version=progress_claim.record.run_state_version,
            occurred_at=datetime.now(UTC),
            sequence=1,
            status="running",
        )
    )
    await service._tickets.release_after_progress(
        first.execution_ticket,
        owner="progress-callback-worker",
        lease_token=progress_claim.record.lease_token or "",
        run_state_version=progressed.run.state_version,
        event_sequence=progressed.run.event_sequence,
    )
    restarted_service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=service._delegated_runs,
        tickets=service._tickets,
        ticket_ttl_seconds=60,
    )

    retried = await restarted_service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    resumed = await service._tickets.resolve_bound(retried.execution_ticket, purpose="agent_event")
    assert retried.execution_ticket == first.execution_ticket
    assert resumed.run_state_version == progressed.run.state_version
    assert resumed.event_sequence == progressed.run.event_sequence

    completion_claim = await service._tickets.claim(
        retried.execution_ticket,
        tenant_id="tenant-1",
        user_id="external-user",
        purpose="agent_event",
        owner="completion-callback-worker",
        lease_seconds=30,
    )
    completed = await service._delegated_runs.complete(
        DelegatedRunCompleteCommand(
            event_id="event_external_complete",
            run_id=retried.run.run_id,
            turn_id=retried.run.turn_id,
            tenant_id="tenant-1",
            user_id="external-user",
            agent_id="external-agent",
            expected_state_version=completion_claim.record.run_state_version,
            occurred_at=datetime.now(UTC),
            result_id="result_external_complete",
            response_text="External work completed.",
        )
    )
    consumed = await service._tickets.consume(
        retried.execution_ticket,
        event_id="event_external_complete",
        owner="completion-callback-worker",
        lease_token=completion_claim.record.lease_token or "",
    )
    assert completed.run.status == "completed"
    assert consumed.status.value == "consumed"


async def test_independent_external_services_share_acceptance_idempotency_and_one_active_ticket() -> (
    None
):
    acceptances = MemoryExternalExecutionAcceptanceStore()
    executor = _ConcurrentAcceptanceExecutor(
        OacExternalExecutor(
            supported_executor_refs={"host_executor"},
            acceptance_store=acceptances,
            acceptance_fingerprint_secret="external-test-secret",
        )
    )
    request, route = await _routed_external_response(executor, tenant_id="oac")
    first_service, runs, ticket_store, turns = await _service(executor)
    second_service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=first_service._delegated_runs,
        tickets=first_service._tickets,
        ticket_ttl_seconds=60,
    )
    turn = await turns.start_turn(
        tenant_id="oac",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    deadline = datetime.now(UTC) + timedelta(minutes=5)

    first, second = await asyncio.gather(
        first_service.start_from_route(
            request,
            route,
            turn_id=turn.turn.turn_id,
            deadline_at=deadline,
        ),
        second_service.start_from_route(
            request,
            route,
            turn_id=turn.turn.turn_id,
            deadline_at=deadline,
        ),
    )

    assert executor.accept_calls == 2
    assert len(acceptances.records) == 1
    assert first.run.run_id == second.run.run_id
    assert first.execution_ticket == second.execution_ticket
    assert len(runs.runs) == 1
    active_records = [
        record
        for record in ticket_store.records.values()
        if record.claims.run_id == first.run.run_id and record.status.value in {"issued", "claimed"}
    ]
    assert len(active_records) == 1


async def test_database_external_acceptance_is_durable_across_store_instances(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'external-acceptances.db'}",
    )
    await managed_database.initialize_schema(settings)
    reservation = ExternalExecutionAcceptanceReservation(
        acceptance_id="external_acceptance_restart",
        request_fingerprint="a" * 64,
        executor_ref="host_executor",
    )
    first_store = DatabaseExternalExecutionAcceptanceStore(
        await managed_database.session_factory(settings)
    )
    restarted_store = DatabaseExternalExecutionAcceptanceStore(
        await managed_database.session_factory(settings)
    )

    assert await first_store.record_accepted(reservation) is True
    assert await restarted_store.record_accepted(reservation) is False
    with pytest.raises(ExternalExecutionAcceptanceConflict):
        await restarted_store.record_accepted(
            reservation.model_copy(update={"executor_ref": "other_executor"})
        )


@pytest.mark.asyncio
async def test_ticket_issue_failure_uses_failure_terminalization_without_cancellation() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, runs, _, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    delegated_runs = _CancellationForbiddenDelegatedRuns(service._delegated_runs)
    service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=delegated_runs,
        tickets=_TicketIssuerFailure(),
        ticket_ttl_seconds=60,
    )

    with pytest.raises(ExternalExecutionBindingUnavailableError) as exc_info:
        await service.start_from_route(
            request,
            route,
            turn_id=turn.turn.turn_id,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    assert exc_info.value.details == {"reason_code": "execution_ticket_unavailable"}
    assert len(runs.runs) == 1
    run = next(iter(runs.runs.values()))
    assert run.status == "failed"
    assert run.error == {"code": "execution_ticket_issue_failed"}
    assert delegated_runs.cancel_calls == 0
    canonical_turn = await turns.get_turn(
        turn_id=turn.turn.turn_id,
        tenant_id="tenant-1",
        user_id="external-user",
    )
    assert canonical_turn is not None
    assert canonical_turn.status.value == "failed"


@pytest.mark.asyncio
async def test_external_execution_recovers_a_ticket_committed_before_the_response_failed() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    recovering_service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=service._delegated_runs,
        tickets=_CommitThenRaiseTicketIssuer(service._tickets),
        ticket_ttl_seconds=60,
    )

    started = await recovering_service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    run = await runs.get_run(started.run.run_id)
    assert run is not None
    assert run.status == "pending"
    assert len(ticket_store.records) == 1
    assert await service._tickets.resolve_bound(
        started.execution_ticket,
        purpose="agent_event",
    )


@pytest.mark.asyncio
async def test_external_execution_retry_recovers_an_unknown_committed_ticket() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    first = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    retried_service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=service._delegated_runs,
        tickets=_CommitThenRaiseTicketIssuer(service._tickets),
        ticket_ttl_seconds=60,
    )

    retried = await retried_service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert retried.execution_ticket == first.execution_ticket
    run = await runs.get_run(first.run.run_id)
    assert run is not None
    assert run.status == "pending"
    assert len(ticket_store.records) == 1


@pytest.mark.asyncio
async def test_failed_first_external_ticket_issue_cannot_terminate_a_successful_retry() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )
    tickets = _FirstTicketIssueFails(service._tickets)
    first_service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=service._delegated_runs,
        tickets=tickets,
        ticket_ttl_seconds=60,
    )
    second_service = ExternalExecutionService(
        external_executor=executor,
        delegated_runs=service._delegated_runs,
        tickets=tickets,
        ticket_ttl_seconds=60,
    )
    deadline = datetime.now(UTC) + timedelta(minutes=5)

    first_task = asyncio.create_task(
        first_service.start_from_route(
            request,
            route,
            turn_id=turn.turn.turn_id,
            deadline_at=deadline,
        )
    )
    await tickets.first_issue_started.wait()
    retried = await second_service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=deadline,
    )
    tickets.release_first_issue.set()
    first = await first_task

    assert first.run.run_id == retried.run.run_id
    assert first.execution_ticket == retried.execution_ticket
    run = await runs.get_run(first.run.run_id)
    assert run is not None
    assert run.status == "pending"
    assert len(ticket_store.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supported", "acceptance", "reason_code"),
    [
        (False, None, "external_executor_unsupported"),
        (
            True,
            ExternalExecutorAcceptance(
                accepted=False,
                reason_code="external_executor_unauthorized",
            ),
            "external_executor_unauthorized",
        ),
        (
            True,
            ExternalExecutorAcceptance(
                accepted=False,
                reason_code="external_executor_unhealthy",
            ),
            "external_executor_unhealthy",
        ),
    ],
)
async def test_rejected_external_executor_creates_no_run_or_ticket(
    supported: bool,
    acceptance: ExternalExecutorAcceptance | None,
    reason_code: str,
) -> None:
    # Snapshot construction catches a permanently unsupported reference. This
    # path proves a Host's later availability/authorization rejection still
    # happens before any canonical external-execution side effect.
    executor = _Executor(supported=True, acceptance=acceptance)
    request, route = await _routed_external_response(executor)
    executor.supported = supported
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )

    with pytest.raises(ExternalExecutionBindingUnavailableError) as exc_info:
        await service.start_from_route(
            request,
            route,
            turn_id=turn.turn.turn_id,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    assert exc_info.value.details == {"reason_code": reason_code}
    assert runs.runs == {}
    assert ticket_store.records == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_binding_token",
    ["token_abcdefghijk", "password_super_secret_123"],
)
async def test_external_executor_binding_token_is_fingerprinted_before_persistence(
    raw_binding_token: str,
) -> None:
    executor = _Executor(
        acceptance=ExternalExecutorAcceptance(accepted=True, binding_id=raw_binding_token)
    )
    request, route = await _routed_external_response(executor)
    service, runs, _, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )

    started = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    run = await runs.get_run(started.run.run_id)
    assert run.binding_snapshot is not None
    assert run.binding_snapshot.executor_binding_id == external_execution_binding_fingerprint(
        raw_binding_token,
        secret="external-test-secret",
    )
    assert raw_binding_token not in run.binding_snapshot.model_dump_json()
    assert raw_binding_token not in started.binding_trace_facts.values()


@pytest.mark.asyncio
async def test_external_execution_rejects_a_tampered_route_before_side_effects() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    route.decision.target_agent_id = "another-agent"
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )

    with pytest.raises(ExternalExecutionBindingUnavailableError):
        await service.start_from_route(
            request,
            route,
            turn_id=turn.turn.turn_id,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    assert executor.requests == []
    assert runs.runs == {}
    assert ticket_store.records == {}


@pytest.mark.asyncio
async def test_external_execution_rejects_replaced_snapshot_before_side_effects() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    replacement = (
        RegistrySnapshotBuilder(None, external_executor=executor)
        .build([_definition()], source="replacement-external-execution-test")
        .select_for_user("external-agent", request.user)
    )
    assert replacement is not None
    route._selected_bindings["external-agent"] = replacement
    service, runs, ticket_store, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )

    with pytest.raises(ExternalExecutionBindingUnavailableError):
        await service.start_from_route(
            request,
            route,
            turn_id=turn.turn.turn_id,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    assert executor.requests == []
    assert runs.runs == {}
    assert ticket_store.records == {}


@pytest.mark.asyncio
async def test_external_execution_uses_frozen_action_plan_facts() -> None:
    executor = _Executor()
    request, route = await _routed_external_response(executor)
    # These are ordinary mutable wire/request fields. They must not be able to
    # retarget the immutable External Execution capability captured by Router.
    request.plan_id = "tampered-request-plan"
    request.step_id = "tampered-request-step"
    route.plan = Plan(
        plan_id="tampered-response-plan",
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        status="running",
        current_step_id="tampered-response-step",
        steps=[
            PlanStep(
                step_id="tampered-response-step",
                agent_id="external-agent",
                description="must not be used by External Execution",
                status="running",
            )
        ],
    )
    assert route.has_trusted_external_execution_for(request)
    service, _, _, turns = await _service(executor)
    turn = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="external-user",
        session_id=request.session_id,
        request_id=request.request_id or "",
        source=request.source,
        user_input=TurnUserInput(text=request.input.text),
    )

    started = await service.start_from_route(
        request,
        route,
        turn_id=turn.turn.turn_id,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert started.run.plan_id is None
    assert started.run.step_id is None


@pytest.mark.asyncio
async def test_plan_execution_returns_wait_for_selected_v2_external_handling() -> None:
    from app.schemas.agents import AgentDefinitionV2
    from app.schemas.common import UserContext
    from app.schemas.plans import Plan, PlanStep

    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = Plan(
        plan_id="external-plan",
        tenant_id="tenant-1",
        user_id="external-user",
        session_id="external-session",
        status="running",
        steps=[
            PlanStep(step_id="external-step", agent_id="external-agent", description="delegate")
        ],
    )
    await plans.save(plan)
    executor = PlanExecutor(
        plan_service=plan_service,
        registry=SimpleNamespace(),
        invocation_service=SimpleNamespace(),
    )

    response = await executor.execute(
        plan.plan_id,
        user=UserContext(
            id="external-user",
            roles=["operator"],
            attributes={"tenant_id": "tenant-1"},
        ),
        selected_definitions={"external-agent": AgentDefinitionV2.model_validate(_definition())},
    )

    assert response.results == []
    assert response.plan.status == "blocked"
    assert response.next_action is not None
    assert response.next_action.type == "wait_for_agent_event"
    assert response.next_action.metadata == {"handling_kind": "external_execution"}


@pytest.mark.parametrize(
    ("executor_ref", "binding_id"),
    [
        ("https://executor.example", "external_binding"),
        ("host_executor", "sk_live_not_a_safe_binding_identifier"),
    ],
)
def test_external_execution_run_binding_rejects_endpoints_and_credentials(
    executor_ref: str,
    binding_id: str,
) -> None:
    with pytest.raises(ValidationError):
        ExternalExecutionBindingSnapshot(
            executor_ref=executor_ref,
            executor_binding_id=binding_id,
        )


def test_external_execution_models_reject_raw_binding_tokens() -> None:
    with pytest.raises(ValidationError, match="non-reversible"):
        ExternalExecutionBindingSnapshot(
            executor_ref="host_executor",
            executor_binding_id="password_super_secret_123",
        )

    with pytest.raises(ValidationError, match="logical binding identifier"):
        ExecutionTraceEventDraft(
            trace_id="trace_external-turn",
            tenant_id="tenant-1",
            user_id="external-user",
            session_id="external-session",
            turn_id="external-turn",
            run_id="external-run",
            event_type="agent_run",
            stage="accepted",
            status="pending",
            source="oir:external_execution",
            source_event_id="external-run:accepted-ref",
            facts={
                "agent_id": "external-agent",
                "handling_kind": "external_execution",
                "executor_ref": "https://host.example/private?token=x",
            },
        )

    with pytest.raises(ValidationError, match="non-reversible"):
        ExecutionTraceEventDraft(
            trace_id="trace_external-turn",
            tenant_id="tenant-1",
            user_id="external-user",
            session_id="external-session",
            turn_id="external-turn",
            run_id="external-run",
            event_type="agent_run",
            stage="accepted",
            status="pending",
            source="oir:external_execution",
            source_event_id="external-run:accepted",
            facts={
                "agent_id": "external-agent",
                "handling_kind": "external_execution",
                "executor_ref": "host_executor",
                "executor_binding_id": "password_super_secret_123",
            },
        )


def test_external_binding_projection_cannot_be_verified_without_the_secret() -> None:
    projected = external_execution_binding_fingerprint(
        "password_super_secret_123",
        secret="private-binding-projection-key",
    )

    assert projected != external_execution_binding_fingerprint(
        "password_super_secret_123",
        secret="attacker-does-not-know-the-key",
    )
