from datetime import UTC, datetime
from typing import Literal

from app.repositories.delegated_runs import DelegatedRunStartConflict
from app.schemas.delegated_runs import (
    DelegatedRunCancelCommand,
    DelegatedRunCompleteCommand,
    DelegatedRunFailCommand,
    DelegatedRunProgressCommand,
)
from app.schemas.events import AgentEvent, AgentEventResponse
from app.schemas.execution_tickets import ExecutionTicketClaims, ExecutionTicketRecord
from app.services.delegated_run_service import DelegatedRunService
from app.services.event_service import EventService
from app.services.execution_ticket_service import ExecutionTicketError, ExecutionTicketService
from app.services.plan_service import PlanService, validate_agent_event_contract

EXECUTION_TICKET_PURPOSE = "agent_event"

AgentEventRejectionCategory = Literal[
    "unauthorized",
    "conflict",
    "invalid",
    "not_found",
]


class AgentEventRejected(ValueError):
    def __init__(self, category: AgentEventRejectionCategory, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


class NativeAgentEventService:
    def __init__(
        self,
        *,
        tickets: ExecutionTicketService,
        delegated_runs: DelegatedRunService,
        run_repository,
        ticket_lease_seconds: int,
    ) -> None:
        self.tickets = tickets
        self.delegated_runs = delegated_runs
        self.run_repository = run_repository
        self.ticket_lease_seconds = ticket_lease_seconds

    async def record(
        self,
        payload: AgentEvent,
        *,
        execution_ticket: str | None,
        path_run_id: str | None = None,
    ) -> AgentEventResponse:
        if not execution_ticket:
            raise AgentEventRejected("unauthorized", "execution_ticket_required")
        try:
            resolved = await self.tickets.resolve_bound(
                execution_ticket,
                purpose=EXECUTION_TICKET_PURPOSE,
            )
        except ExecutionTicketError as exc:
            raise AgentEventRejected("unauthorized", "execution_ticket_invalid") from exc

        _validate_ticket_correlation(payload, path_run_id=path_run_id, claims=resolved.claims)
        run = await _validate_canonical_run(
            payload,
            record=resolved,
            run_repository=self.run_repository,
        )
        try:
            validate_agent_event_contract(payload)
        except ValueError as exc:
            raise AgentEventRejected("invalid", "agent_event_status_conflict") from exc

        owner = f"native-agent-event:{payload.event_id}"
        try:
            claim = await self.tickets.claim(
                execution_ticket,
                tenant_id=resolved.claims.tenant_id,
                user_id=resolved.claims.user_id,
                purpose=EXECUTION_TICKET_PURPOSE,
                owner=owner,
                lease_seconds=self.ticket_lease_seconds,
            )
        except ExecutionTicketError as exc:
            category: AgentEventRejectionCategory = (
                "conflict" if "claimed" in str(exc).lower() else "unauthorized"
            )
            raise AgentEventRejected(category, "execution_ticket_unavailable") from exc

        record = claim.record
        occurred_at = _resolve_event_occurrence(
            payload,
            record=record,
            run=run,
        )
        if claim.duplicate:
            return await self._validate_consumed_replay(
                payload,
                record=record,
                occurred_at=occurred_at,
            )

        lease_token = record.lease_token
        if not lease_token:
            raise AgentEventRejected("conflict", "execution_ticket_claim_invalid")

        try:
            result, terminal = await _apply_delegated_event(
                payload,
                record=record,
                occurred_at=occurred_at,
                delegated_runs=self.delegated_runs,
            )
            if terminal:
                await self.tickets.consume(
                    execution_ticket,
                    event_id=payload.event_id,
                    owner=owner,
                    lease_token=lease_token,
                )
            elif result.duplicate:
                event_sequence = payload.sequence or record.event_sequence + 1
                if (
                    result.run.state_version > record.run_state_version
                    and event_sequence > record.event_sequence
                ):
                    await self.tickets.release_after_progress(
                        execution_ticket,
                        owner=owner,
                        lease_token=lease_token,
                        run_state_version=result.run.state_version,
                        event_sequence=event_sequence,
                    )
                elif (
                    result.run.state_version == record.run_state_version
                    and event_sequence == record.event_sequence
                ):
                    await self.tickets.release_claim(
                        execution_ticket,
                        owner=owner,
                        lease_token=lease_token,
                    )
                else:
                    raise ExecutionTicketError("Execution ticket cursor conflict")
            else:
                await self.tickets.release_after_progress(
                    execution_ticket,
                    owner=owner,
                    lease_token=lease_token,
                    run_state_version=result.run.state_version,
                    event_sequence=payload.sequence or record.event_sequence + 1,
                )
        except (DelegatedRunStartConflict, ExecutionTicketError, ValueError) as exc:
            await _release_rejected_claim(
                self.tickets,
                execution_ticket,
                owner=owner,
                lease_token=lease_token,
            )
            raise AgentEventRejected("conflict", "agent_event_conflict") from exc

        return AgentEventResponse(
            event_id=payload.event_id,
            accepted=True,
            duplicate=result.duplicate,
            message="Agent event committed",
        )

    async def _validate_consumed_replay(
        self,
        payload: AgentEvent,
        *,
        record: ExecutionTicketRecord,
        occurred_at: datetime,
    ) -> AgentEventResponse:
        if record.consumed_event_id != payload.event_id:
            raise AgentEventRejected("conflict", "execution_ticket_consumed")
        try:
            result, terminal = await _apply_delegated_event(
                payload,
                record=record,
                occurred_at=occurred_at,
                delegated_runs=self.delegated_runs,
            )
        except (DelegatedRunStartConflict, ExecutionTicketError, ValueError) as exc:
            raise AgentEventRejected("conflict", "agent_event_conflict") from exc
        if not terminal or not result.duplicate:
            raise AgentEventRejected("conflict", "agent_event_conflict")
        return AgentEventResponse(
            event_id=payload.event_id,
            accepted=True,
            duplicate=True,
            message="Agent event already committed",
        )


async def record_trusted_agent_event(
    payload: AgentEvent,
    *,
    event_service: EventService,
    plan_service: PlanService,
    run_repository,
) -> AgentEventResponse:
    """Record an in-process event without exposing a second HTTP authority path."""
    _validate_trusted_event_contract(payload)
    plan = None
    formation_suppressed = False
    if payload.plan_id:
        plan, run = await _resolve_trusted_plan_event_target(
            payload,
            plan_service=plan_service,
            run_repository=run_repository,
        )
        formation_suppressed = run.formation_suppressed
    event = await _bind_trusted_event_owner(
        payload,
        plan=plan,
        run_repository=run_repository,
    )
    response = await event_service.record_agent_event(event)
    stored_event = await _stored_trusted_event_or_error(event_service, event)
    if plan is not None:
        await plan_service.apply_agent_event(
            stored_event,
            tenant_id=plan.tenant_id,
            user_id=plan.user_id,
            publish=not formation_suppressed,
        )
    return response


async def _apply_delegated_event(
    payload: AgentEvent,
    *,
    record: ExecutionTicketRecord,
    occurred_at: datetime,
    delegated_runs: DelegatedRunService,
):
    claims = record.claims
    common = {
        "event_id": payload.event_id,
        "run_id": claims.run_id,
        "turn_id": claims.turn_id,
        "tenant_id": claims.tenant_id,
        "user_id": claims.user_id,
        "agent_id": claims.agent_id,
        "plan_id": claims.plan_id,
        "step_id": claims.step_id,
        "expected_state_version": record.run_state_version,
        "occurred_at": occurred_at,
    }
    if payload.event_type in {"agent_started", "agent_progress", "agent_clarify"}:
        result = await delegated_runs.progress(
            DelegatedRunProgressCommand(
                **common,
                event_type=payload.event_type,
                sequence=payload.sequence or record.event_sequence + 1,
                status="blocked" if payload.event_type == "agent_clarify" else "running",
                payload=payload.payload,
            )
        )
        return result, False
    if payload.event_type == "agent_result":
        output = payload.payload.get("output")
        artifact_refs = payload.payload.get("artifact_refs", [])
        result = await delegated_runs.complete(
            DelegatedRunCompleteCommand(
                **common,
                result_id=str(payload.payload.get("result_id") or f"result_{payload.event_id}"),
                response_text=str(
                    payload.payload.get("message") or payload.payload.get("response_text") or ""
                ),
                output=output if isinstance(output, dict) else payload.payload,
                artifact_refs=(
                    [item for item in artifact_refs if isinstance(item, dict)]
                    if isinstance(artifact_refs, list)
                    else []
                ),
            )
        )
        return result, True
    if payload.event_type == "agent_error":
        error = payload.payload.get("error")
        result = await delegated_runs.fail(
            DelegatedRunFailCommand(
                **common,
                error=error if isinstance(error, dict) else payload.payload,
            )
        )
        return result, True
    if payload.event_type == "agent_cancelled":
        result = await delegated_runs.cancel(
            DelegatedRunCancelCommand(
                **common,
                reason=str(payload.payload.get("reason") or "execution_cancelled"),
            )
        )
        return result, True
    raise ValueError("Unsupported Agent Event type")


def _validate_ticket_correlation(
    payload: AgentEvent,
    *,
    path_run_id: str | None,
    claims: ExecutionTicketClaims,
) -> None:
    supplied = {
        "run_id": path_run_id or payload.run_id,
        "request_id": payload.request_id,
        "turn_id": payload.turn_id,
        "tenant_id": payload.tenant_id,
        "user_id": payload.user_id,
        "agent_id": payload.agent_id,
        "plan_id": payload.plan_id,
        "step_id": payload.step_id,
    }
    if path_run_id and payload.run_id and path_run_id != payload.run_id:
        raise AgentEventRejected("conflict", "agent_event_identity_conflict")
    for field, supplied_value in supplied.items():
        if supplied_value is not None and supplied_value != getattr(claims, field):
            raise AgentEventRejected("conflict", "agent_event_identity_conflict")


async def _validate_canonical_run(
    payload: AgentEvent,
    *,
    record: ExecutionTicketRecord,
    run_repository,
):
    run = await run_repository.get_run(record.claims.run_id)
    claims = record.claims
    if (
        run is None
        or not run.delegated
        or run.request_id != claims.request_id
        or run.turn_id != claims.turn_id
        or run.tenant_id != claims.tenant_id
        or run.user_id != claims.user_id
        or run.agent_id != claims.agent_id
        or run.plan_id != claims.plan_id
        or run.step_id != claims.step_id
        or run.session_id != payload.session_id
    ):
        raise AgentEventRejected("conflict", "agent_event_identity_conflict")
    return run


def _resolve_event_occurrence(
    payload: AgentEvent,
    *,
    record: ExecutionTicketRecord,
    run,
) -> datetime:
    is_terminal_replay = (
        payload.event_type in {"agent_result", "agent_error", "agent_cancelled"}
        and run.terminal_event_id == payload.event_id
    )
    if is_terminal_replay:
        if (
            payload.run_state_version is not None
            and payload.run_state_version != record.run_state_version
        ):
            raise AgentEventRejected("conflict", "agent_event_conflict")
        if payload.sequence is not None and payload.sequence != run.event_sequence:
            raise AgentEventRejected("conflict", "agent_event_conflict")
        canonical = _as_utc(run.updated_at)
        if payload.created_at is not None:
            proposed = max(_as_utc(payload.created_at), _as_utc(run.created_at))
            if proposed != canonical:
                raise AgentEventRejected("conflict", "agent_event_conflict")
        return canonical

    if payload.sequence is not None:
        if payload.sequence == record.event_sequence:
            expected_client_version = record.run_state_version - 1
        elif payload.sequence == record.event_sequence + 1:
            expected_client_version = record.run_state_version
        else:
            raise AgentEventRejected("conflict", "agent_event_conflict")
        if (
            payload.run_state_version is not None
            and payload.run_state_version != expected_client_version
        ):
            raise AgentEventRejected("conflict", "agent_event_conflict")
    elif (
        payload.run_state_version is not None
        and payload.run_state_version != record.run_state_version
    ):
        raise AgentEventRejected("conflict", "agent_event_conflict")
    proposed = _as_utc(payload.created_at or datetime.now(UTC))
    return max(proposed, _as_utc(run.created_at))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _stored_trusted_event_or_error(
    event_service: EventService,
    incoming: AgentEvent,
) -> AgentEvent:
    stored = await event_service.get_event(
        incoming.event_id,
        tenant_id=incoming.tenant_id,
        user_id=incoming.user_id,
    )
    if stored is None:
        raise AgentEventRejected("conflict", "Agent event identity conflict")
    identity_fields = (
        "run_id",
        "session_id",
        "agent_id",
        "user_id",
        "tenant_id",
        "plan_id",
        "step_id",
        "event_type",
        "status",
    )
    if any(getattr(stored, field) != getattr(incoming, field) for field in identity_fields):
        raise AgentEventRejected("conflict", "Agent event identity conflict")
    return stored


def _validate_trusted_event_contract(event: AgentEvent) -> None:
    try:
        validate_agent_event_contract(event)
    except ValueError as exc:
        raise AgentEventRejected("invalid", str(exc)) from exc


async def _bind_trusted_event_owner(
    event: AgentEvent,
    *,
    plan,
    run_repository,
) -> AgentEvent:
    if plan is not None:
        return event.model_copy(update={"user_id": plan.user_id, "tenant_id": plan.tenant_id})
    if not event.run_id:
        return event.model_copy(update={"user_id": None, "tenant_id": None})
    run = await run_repository.get_run(event.run_id)
    if run is None or run.session_id != event.session_id or run.agent_id != event.agent_id:
        raise AgentEventRejected("not_found", "Run not found")
    return event.model_copy(update={"user_id": run.user_id, "tenant_id": run.tenant_id})


async def _resolve_trusted_plan_event_target(
    event: AgentEvent,
    *,
    plan_service: PlanService,
    run_repository,
):
    if not event.run_id:
        raise AgentEventRejected("invalid", "Trusted Run association is required")
    run = await run_repository.get_run(event.run_id)
    if (
        run is None
        or not run.user_id
        or not run.tenant_id
        or run.session_id != event.session_id
        or run.agent_id != event.agent_id
        or run.plan_id != event.plan_id
        or run.step_id != event.step_id
    ):
        raise AgentEventRejected("not_found", "Plan not found")
    plan = await plan_service.get_plan(
        event.plan_id,
        tenant_id=run.tenant_id,
        user_id=run.user_id,
    )
    if (
        plan is None
        or plan.session_id != event.session_id
        or not any(
            step.step_id == event.step_id and step.agent_id == event.agent_id for step in plan.steps
        )
    ):
        raise AgentEventRejected("not_found", "Plan not found")
    return plan, run


async def _release_rejected_claim(
    tickets: ExecutionTicketService,
    ticket: str,
    *,
    owner: str,
    lease_token: str,
) -> None:
    try:
        await tickets.release_claim(
            ticket,
            owner=owner,
            lease_token=lease_token,
        )
    except ExecutionTicketError:
        return
