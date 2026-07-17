from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.schemas.common import UserContext
from app.schemas.delegated_runs import (
    DelegatedRunCompleteCommand,
    DelegatedRunProgressCommand,
    DelegatedRunStartCommand,
)
from app.schemas.execution_tickets import LegacyExecutionCorrelationQuery
from app.schemas.turns import TurnUserInput
from app.services.execution_ticket_service import ExecutionTicketError, ExecutionTicketService
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.cutover import CutoverGuard
from host_adapters.oac.fallback.gateway import (
    FallbackBlockedError,
    IRSFallbackGateway,
    IRSLegacyClient,
)
from host_adapters.oac.fallback.policy import CommitStatus, classify_operation
from host_adapters.oac.identity import HostOperation, authorize_host_operation
from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_adapters.oac.mappers.central import (
    agent_event_to_native,
    navigation_event_to_native,
    plan_confirm_to_compat,
    project_error,
    route_request_to_native,
    route_response_to_compat,
)
from host_adapters.oac.schemas.central import (
    AcceptedResponse,
    AgentEventCompatResponse,
    AgentEventRequest,
    CentralRouteRequest,
    CentralRouteResponse,
    NavigationEventRequest,
    PlanConfirmResponse,
)
from host_apps.oac.config import OacHostSettings, get_oac_host_settings
from host_apps.oac.dependencies import (
    get_cutover_guard,
    get_execution_ticket_service,
    get_irs_fallback_gateway,
    get_irs_legacy_client,
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

router = APIRouter(prefix="/api/v1/central", tags=["legacy-central"])


@router.post("/route", response_model=CentralRouteResponse)
async def central_route(
    request: CentralRouteRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    tickets: ExecutionTicketService = Depends(get_execution_ticket_service),
    settings: OacHostSettings = Depends(get_oac_host_settings),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> CentralRouteResponse:
    _authorize(identity, "route_stateful")
    user = _user(identity)
    native_request = route_request_to_native(request, user=user)

    async def primary() -> CentralRouteResponse:
        native_response = await ports.routing.route(native_request)
        ticket = None
        if native_response.decision.action in {"open_agent", "continue_agent"}:
            turn = await ports.turns.start_turn(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=request.session_id,
                request_id=native_response.request_id,
                source=native_request.source,
                user_input=TurnUserInput(
                    text=native_request.input.text,
                    metadata={"input_type": "text", "attachment_count": 0},
                ),
            )
            deadline = datetime.now(UTC) + timedelta(seconds=settings.execution_ticket_ttl_seconds)
            started = await ports.delegated_runs.start(
                DelegatedRunStartCommand(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=request.session_id,
                    request_id=native_response.request_id,
                    turn_id=turn.turn.turn_id,
                    agent_id=native_response.decision.target_agent_id or "",
                    plan_id=native_response.plan.plan_id
                    if native_response.plan
                    else request.plan_id,
                    step_id=(
                        native_response.plan.current_step_id
                        if native_response.plan
                        else request.step_id
                    ),
                    deadline_at=deadline,
                    input=native_response.invocation.input if native_response.invocation else {},
                )
            )
            issued = await tickets.issue(
                started.run,
                request_id=native_response.request_id,
                purpose="agent_event",
                ttl_seconds=settings.execution_ticket_ttl_seconds,
            )
            ticket = issued.ticket
        return route_response_to_compat(
            native_response,
            source=request.source,
            execution_ticket=ticket,
        )

    async def fallback() -> CentralRouteResponse:
        payload = await irs.request_json(
            method="POST",
            path="/api/v1/central/route",
            json_body=request.model_dump(mode="json", by_alias=True),
        )
        return CentralRouteResponse.model_validate(payload)

    async def commit_probe() -> CommitStatus:
        status = await ports.turns.submission_status(
            request_id=request.request_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        return CommitStatus(status)

    try:
        return await fallback_gateway.execute(
            operation=classify_operation("POST", "/api/v1/central/route"),
            request_id=request.request_id,
            primary=primary,
            fallback=fallback,
            commit_probe=commit_probe,
            correlation={
                "request_id": request.request_id,
                "session_id": request.session_id,
                "plan_id": request.plan_id,
            },
        )
    except FallbackBlockedError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "fallback_blocked", "reason": exc.reason, "retryable": True},
        ) from exc
    except Exception as exc:
        _raise_projected(exc)


@router.post("/events/navigation", status_code=202, response_model=AcceptedResponse)
async def navigation_event(
    request: NavigationEventRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> AcceptedResponse:
    _authorize(identity, "runtime_write_own")
    event = navigation_event_to_native(
        request,
        user=_user(identity),
        event_id=f"navigation_{uuid4().hex}",
    )
    await ports.events.record_conversation_event(event)
    return AcceptedResponse(accepted=True)


@router.post("/events/agent", response_model=AgentEventCompatResponse)
async def agent_event(
    request: AgentEventRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    tickets: ExecutionTicketService = Depends(get_execution_ticket_service),
    settings: OacHostSettings = Depends(get_oac_host_settings),
    cutover: CutoverGuard = Depends(get_cutover_guard),
) -> AgentEventCompatResponse:
    _authorize(identity, "runtime_write_own")
    if await cutover.quarantine_if_legacy(
        event_id=request.event_id,
        session_id=request.session_id,
        user_id=identity.user_id,
        agent_id=request.agent_id,
        occurred_at=request.created_at,
    ):
        return AgentEventCompatResponse(
            event_id=request.event_id,
            session_id=request.session_id,
            accepted=False,
            route_required=False,
        )
    owner = f"agent-event:{request.event_id}"
    try:
        if request.execution_ticket:
            claim = await tickets.claim(
                request.execution_ticket,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                purpose="agent_event",
                owner=owner,
                lease_seconds=settings.execution_ticket_lease_seconds,
            )
            raw_ticket = request.execution_ticket
        else:
            claim = await tickets.claim_legacy(
                LegacyExecutionCorrelationQuery(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    agent_id=request.agent_id,
                    plan_id=request.plan_id,
                    step_id=request.step_id,
                    event_id=request.event_id,
                    purpose="agent_event",
                    now=request.created_at or datetime.now(UTC),
                ),
                owner=owner,
                lease_seconds=settings.execution_ticket_lease_seconds,
            )
            raw_ticket = None
        record = claim.record
        occurred_at = request.created_at or datetime.now(UTC)
        if await cutover.quarantine_if_legacy(
            event_id=request.event_id,
            session_id=request.session_id,
            user_id=identity.user_id,
            agent_id=request.agent_id,
            occurred_at=request.created_at,
            ticket_expires_at=record.claims.expires_at,
            ticket_ttl_seconds=settings.execution_ticket_ttl_seconds,
        ):
            return AgentEventCompatResponse(
                event_id=request.event_id,
                session_id=request.session_id,
                accepted=False,
                route_required=False,
            )
        if request.event_type == "agent_progress" or request.status in {"running", "blocked"}:
            sequence = record.event_sequence + 1
            result = await ports.delegated_runs.progress(
                DelegatedRunProgressCommand(
                    event_id=request.event_id,
                    run_id=record.claims.run_id,
                    turn_id=record.claims.turn_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    agent_id=request.agent_id,
                    plan_id=request.plan_id,
                    step_id=request.step_id,
                    expected_state_version=record.run_state_version,
                    occurred_at=occurred_at,
                    sequence=sequence,
                    status="blocked" if request.status == "blocked" else "running",
                    payload={"message": request.message, "output": request.output},
                )
            )
            if raw_ticket and record.lease_token:
                await tickets.release_after_progress(
                    raw_ticket,
                    owner=owner,
                    lease_token=record.lease_token,
                    run_state_version=result.run.state_version,
                    event_sequence=sequence,
                    now=occurred_at,
                )
            elif record.lease_token:
                await tickets.release_legacy_after_progress(
                    record.ticket_hash,
                    owner=owner,
                    lease_token=record.lease_token,
                    run_state_version=result.run.state_version,
                    event_sequence=sequence,
                    now=occurred_at,
                )
        elif request.status == "completed":
            result = await ports.delegated_runs.complete(
                DelegatedRunCompleteCommand(
                    event_id=request.event_id,
                    run_id=record.claims.run_id,
                    turn_id=record.claims.turn_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    agent_id=request.agent_id,
                    plan_id=request.plan_id,
                    step_id=request.step_id,
                    expected_state_version=record.run_state_version,
                    occurred_at=occurred_at,
                    result_id=request.result_ref or f"result_{request.event_id}",
                    response_text=request.message,
                    output=request.output if isinstance(request.output, dict) else None,
                    artifact_refs=[
                        {"artifact_id": item, "uri": item} for item in request.artifact_refs
                    ],
                )
            )
            if raw_ticket and record.lease_token:
                await tickets.consume(
                    raw_ticket,
                    event_id=request.event_id,
                    owner=owner,
                    lease_token=record.lease_token,
                    now=occurred_at,
                )
            elif record.lease_token:
                await tickets.consume_legacy(
                    record.ticket_hash,
                    event_id=request.event_id,
                    owner=owner,
                    lease_token=record.lease_token,
                    now=occurred_at,
                )
        else:
            native = agent_event_to_native(
                request,
                user=_user(identity),
                run_id=record.claims.run_id,
                turn_id=record.claims.turn_id,
                request_id=record.claims.request_id,
            )
            recorded = await ports.events.record_agent_event(native)
            result = None
            if raw_ticket and record.lease_token:
                await tickets.consume(
                    raw_ticket,
                    event_id=request.event_id,
                    owner=owner,
                    lease_token=record.lease_token,
                    now=occurred_at,
                )
            elif record.lease_token:
                await tickets.consume_legacy(
                    record.ticket_hash,
                    event_id=request.event_id,
                    owner=owner,
                    lease_token=record.lease_token,
                    now=occurred_at,
                )
            return AgentEventCompatResponse(
                event_id=request.event_id,
                session_id=request.session_id,
                accepted=recorded.accepted,
                duplicate=recorded.duplicate,
                route_required=True,
            )
        return AgentEventCompatResponse(
            event_id=request.event_id,
            session_id=request.session_id,
            accepted=True,
            duplicate=result.duplicate if result else False,
            route_required=True,
        )
    except (ExecutionTicketError, ValueError) as exc:
        _raise_projected(exc)


@router.post("/plans/{plan_id}/confirm", response_model=PlanConfirmResponse)
async def confirm_plan(
    plan_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> PlanConfirmResponse:
    _authorize(identity, "runtime_write_own")
    try:
        plan = await ports.plans.get_plan(
            plan_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        if plan is None:
            raise KeyError(plan_id)
        response = await ports.plans.confirm(
            plan_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        return plan_confirm_to_compat(response, plan=plan)
    except Exception as exc:
        _raise_projected(exc)


def _user(identity: TrustedHostIdentity) -> UserContext:
    return UserContext(
        id=identity.user_id,
        groups=list(identity.groups),
        attributes={"tenant_id": identity.tenant_id},
    )


def _authorize(identity: TrustedHostIdentity, operation: HostOperation) -> None:
    try:
        authorize_host_operation(identity, operation)
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc


def _raise_projected(error: Exception) -> None:
    status_code, body = project_error(error)
    raise HTTPException(status_code=status_code, detail=body.model_dump(mode="json")) from error
