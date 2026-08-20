from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response

from app.core.errors import (
    ExternalExecutionBindingUnavailableError,
    PlanBindingUnavailableError,
)
from app.schemas.common import UserContext
from app.schemas.delegated_runs import (
    DelegatedRunCompleteCommand,
    DelegatedRunFailCommand,
    DelegatedRunProgressCommand,
    DelegatedRunStartCommand,
)
from app.schemas.execution_tickets import LegacyExecutionCorrelationQuery
from app.schemas.execution_traces import ExecutionTraceEventDraft, trace_id_for_turn
from app.schemas.turns import TurnUserInput
from app.services.execution_ticket_service import ExecutionTicketError, ExecutionTicketService
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.authz import OAC_BUNDLE_CATALOG
from host_adapters.oac.cutover import CutoverGuard
from host_adapters.oac.identity import HostOperation, authorize_host_operation
from host_adapters.oac.identity.models import (
    HostAuthenticationError,
    HostAuthorizationError,
    TrustedHostIdentity,
)
from host_adapters.oac.mappers.central import (
    agent_event_to_native,
    navigation_event_to_native,
    plan_confirm_to_compat,
    plan_to_compat,
    project_error,
    route_request_to_native,
    route_response_to_compat,
)
from host_adapters.oac.schemas.central import (
    AcceptedResponse,
    ActivePlanResponse,
    AgentEventCompatResponse,
    AgentEventRequest,
    CentralRouteRequest,
    CentralRouteResponse,
    NavigationEventRequest,
    PlanConfirmRequest,
    PlanConfirmResponse,
)
from host_apps.oac.config import OacHostSettings, get_oac_host_settings
from host_apps.oac.dependencies import (
    get_cutover_guard,
    get_execution_ticket_service,
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

router = APIRouter(prefix="/api/v1/central", tags=["legacy-central"])


async def _plan_event_conflict_response(
    request: AgentEventRequest,
    *,
    identity: TrustedHostIdentity,
    ports: OacAdapterApplicationPorts,
) -> AgentEventCompatResponse | None:
    if request.expected_state_version is None:
        return None
    canonical_plan = (
        await ports.plans.get_plan(
            request.plan_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        if request.plan_id
        else None
    )
    if (
        canonical_plan
        and request.step_id
        and canonical_plan.current_step_id == request.step_id
        and canonical_plan.state_version == request.expected_state_version
        and canonical_plan.status not in {"completed", "failed", "cancelled"}
    ):
        return None
    return AgentEventCompatResponse(
        event_id=request.event_id,
        session_id=request.session_id,
        accepted=False,
        duplicate=True,
        route_required=False,
        conflict=True,
        plan=plan_to_compat(canonical_plan) if canonical_plan else None,
    )


@router.get("/active-plan", response_model=ActivePlanResponse)
async def active_plan(
    session_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> ActivePlanResponse:
    _authorize(identity, "read_only")
    plan = await ports.plans.get_active_plan(
        session_id,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )
    return ActivePlanResponse(plan=plan_to_compat(plan) if plan is not None else None)


@router.post("/route", response_model=CentralRouteResponse)
async def central_route(
    request: CentralRouteRequest,
    response: Response,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    tickets: ExecutionTicketService = Depends(get_execution_ticket_service),
    settings: OacHostSettings = Depends(get_oac_host_settings),
) -> CentralRouteResponse:
    _authorize(identity, "route_stateful")
    if not identity.claims_version:
        raise HTTPException(status_code=401, detail="host_authentication_failed")
    if request.user_id != identity.user_id:
        raise HTTPException(status_code=403, detail="host_claims_mismatch")
    if identity.active_bundle_id:
        try:
            legacy_tag = OAC_BUNDLE_CATALOG.by_id[identity.active_bundle_id].legacy_tag
        except KeyError as exc:
            raise HTTPException(status_code=401, detail="host_authentication_failed") from exc
        if request.user_tags != [legacy_tag]:
            raise HTTPException(status_code=403, detail="host_claims_mismatch")
    user = _user(identity)
    native_request = route_request_to_native(request, user=user)

    async def primary() -> CentralRouteResponse:
        native_response = await ports.routing.route(native_request)
        trace_complete = True
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
        response.headers["X-OIR-Trace-Turn-ID"] = turn.turn.turn_id
        trace_complete = (
            await _record_trace(
                ports,
                ExecutionTraceEventDraft(
                    trace_id=trace_id_for_turn(turn.turn.turn_id),
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=request.session_id,
                    turn_id=turn.turn.turn_id,
                    event_type="canonical_turn",
                    stage="accepted",
                    status=turn.turn.status.value,
                    source="oir:canonical_turn",
                    source_event_id=f"turn:{turn.turn.turn_id}:accepted",
                    facts={
                        "request_id": native_response.request_id,
                        "source": native_request.source,
                        "input_kind": native_request.input.type,
                    },
                    occurred_at=turn.turn.created_at,
                ),
            )
            and trace_complete
        )
        trace_complete = (
            await _record_trace(
                ports,
                ExecutionTraceEventDraft(
                    trace_id=trace_id_for_turn(turn.turn.turn_id),
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=request.session_id,
                    turn_id=turn.turn.turn_id,
                    event_type="route_decision",
                    stage="decision",
                    status=native_response.decision.status,
                    source="oir:route_decision",
                    source_event_id=f"route:{native_response.request_id}",
                    facts={
                        "action": native_response.decision.action,
                        "target_agent_id": native_response.decision.target_agent_id,
                        "candidate_count": len(native_response.context.candidate_agent_ids),
                        "plan_id": native_response.plan.plan_id if native_response.plan else None,
                    },
                ),
            )
            and trace_complete
        )
        trace_complete = (
            await _record_context_and_recall_trace(
                ports,
                response=native_response,
                identity=identity,
                turn_id=turn.turn.turn_id,
            )
            and trace_complete
        )
        ticket = None
        is_external_execution = native_response.is_routed_external_execution()
        if is_external_execution or _requires_delegated_execution(native_response):
            deadline = datetime.now(UTC) + timedelta(seconds=settings.execution_ticket_ttl_seconds)
            if is_external_execution:
                if ports.external_execution is None:
                    raise ExternalExecutionBindingUnavailableError(
                        "External Execution Binding is unavailable",
                        details={"reason_code": "external_executor_unavailable"},
                    )
                external_started = await ports.external_execution.start_from_route(
                    native_request,
                    native_response,
                    turn_id=turn.turn.turn_id,
                    deadline_at=deadline,
                )
                trace_complete = (
                    await _record_trace(
                        ports,
                        ExecutionTraceEventDraft(
                            trace_id=trace_id_for_turn(turn.turn.turn_id),
                            tenant_id=identity.tenant_id,
                            user_id=identity.user_id,
                            session_id=request.session_id,
                            turn_id=turn.turn.turn_id,
                            run_id=external_started.run.run_id,
                            event_type="agent_run",
                            stage="delegated_start",
                            status=str(external_started.run.status),
                            source="oir:agent_run",
                            source_event_id=f"run:{external_started.run.run_id}:started",
                            facts={
                                "agent_id": external_started.run.agent_id,
                                "invoker_type": "external_execution",
                                "delegated": True,
                                **external_started.binding_trace_facts,
                            },
                        ),
                    )
                    and trace_complete
                )
                ticket = external_started.execution_ticket
            else:
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
                        input=native_response.invocation.input
                        if native_response.invocation
                        else {},
                    )
                )
                trace_complete = (
                    await _record_trace(
                        ports,
                        ExecutionTraceEventDraft(
                            trace_id=trace_id_for_turn(turn.turn.turn_id),
                            tenant_id=identity.tenant_id,
                            user_id=identity.user_id,
                            session_id=request.session_id,
                            turn_id=turn.turn.turn_id,
                            run_id=started.run.run_id,
                            event_type="agent_run",
                            stage="delegated_start",
                            status=str(started.run.status),
                            source="oir:agent_run",
                            source_event_id=f"run:{started.run.run_id}:started",
                            facts={
                                "agent_id": started.run.agent_id,
                                "invoker_type": "host_delegated",
                                "delegated": True,
                            },
                        ),
                    )
                    and trace_complete
                )
                issued = await tickets.issue(
                    started.run,
                    request_id=native_response.request_id,
                    purpose="agent_event",
                    ttl_seconds=settings.execution_ticket_ttl_seconds,
                )
                ticket = issued.ticket
        if not trace_complete:
            response.headers["X-OIR-Trace-Completeness"] = "incomplete"
        return route_response_to_compat(
            native_response,
            source=request.source,
            execution_ticket=ticket,
        )

    try:
        return await primary()
    except Exception as exc:
        _raise_projected(exc)


def _requires_delegated_execution(response) -> bool:
    if response.decision.action not in {"open_agent", "continue_agent"}:
        return False
    next_action = response.next_action or (response.plan.next_action if response.plan else None)
    return not (
        response.invocation is None and next_action is not None and next_action.type == "open_ui"
    )


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


@router.post(
    "/events/agent",
    response_model=AgentEventCompatResponse,
    response_model_exclude_none=True,
)
async def agent_event(
    request: AgentEventRequest,
    response: Response,
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
    if conflict := await _plan_event_conflict_response(request, identity=identity, ports=ports):
        return conflict
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
        if request.event_type == "agent_progress" or (
            request.event_type != "agent_error" and request.status in {"running", "blocked"}
        ):
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
            trace_complete = await _record_trace(
                ports,
                ExecutionTraceEventDraft(
                    trace_id=trace_id_for_turn(record.claims.turn_id),
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=request.session_id,
                    turn_id=record.claims.turn_id,
                    run_id=record.claims.run_id,
                    event_type="agent_event",
                    stage="provider_progress",
                    status=str(result.run.status),
                    source="oac:agent_event",
                    source_event_id=request.event_id,
                    facts=_trace_progress_facts(request),
                    occurred_at=occurred_at,
                ),
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
            trace_complete = await _record_trace(
                ports,
                ExecutionTraceEventDraft(
                    trace_id=trace_id_for_turn(record.claims.turn_id),
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=request.session_id,
                    turn_id=record.claims.turn_id,
                    run_id=record.claims.run_id,
                    event_type="agent_event",
                    stage="provider_completed",
                    status="completed",
                    source="oac:agent_event",
                    source_event_id=request.event_id,
                    facts={"agent_id": request.agent_id},
                    occurred_at=occurred_at,
                ),
            )
            trace_complete = (
                await _record_trace(
                    ports,
                    ExecutionTraceEventDraft(
                        trace_id=trace_id_for_turn(record.claims.turn_id),
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        session_id=request.session_id,
                        turn_id=record.claims.turn_id,
                        run_id=record.claims.run_id,
                        event_type="agent_result",
                        stage="result_recorded",
                        status="completed",
                        source="oir:agent_result",
                        source_event_id=f"result:{result.result_id or request.event_id}",
                        facts={
                            "agent_id": request.agent_id,
                            "result_summary": "provider_result_available",
                        },
                        occurred_at=occurred_at,
                    ),
                )
                and trace_complete
            )
        elif request.event_type == "agent_error" or request.status == "failed":
            result = await ports.delegated_runs.fail(
                DelegatedRunFailCommand(
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
                    error=_failure_command_error(request),
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
            failure_facts = _trace_failure_facts(request)
            trace_complete = await _record_trace(
                ports,
                ExecutionTraceEventDraft(
                    trace_id=trace_id_for_turn(record.claims.turn_id),
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=request.session_id,
                    turn_id=record.claims.turn_id,
                    run_id=record.claims.run_id,
                    event_type="agent_event",
                    stage="provider_failed",
                    status="failed",
                    reason_code=failure_facts["error_code"],
                    source="oac:agent_event",
                    source_event_id=request.event_id,
                    facts=failure_facts,
                    occurred_at=occurred_at,
                ),
            )
            trace_complete = (
                await _record_trace(
                    ports,
                    ExecutionTraceEventDraft(
                        trace_id=trace_id_for_turn(record.claims.turn_id),
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        session_id=request.session_id,
                        turn_id=record.claims.turn_id,
                        run_id=record.claims.run_id,
                        event_type="agent_result",
                        stage="result_unavailable",
                        status="failed",
                        reason_code=failure_facts["error_code"],
                        source="oir:agent_result",
                        source_event_id=f"failure:{request.event_id}",
                        facts=failure_facts,
                        occurred_at=occurred_at,
                    ),
                )
                and trace_complete
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
            trace_complete = await _record_trace(
                ports,
                ExecutionTraceEventDraft(
                    trace_id=trace_id_for_turn(record.claims.turn_id),
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=request.session_id,
                    turn_id=record.claims.turn_id,
                    run_id=record.claims.run_id,
                    event_type="agent_event",
                    stage=request.event_type,
                    status=request.status,
                    source="oac:agent_event",
                    source_event_id=request.event_id,
                    facts={"agent_id": request.agent_id},
                    occurred_at=occurred_at,
                ),
            )
            if not trace_complete:
                response.headers["X-OIR-Trace-Completeness"] = "incomplete"
            return AgentEventCompatResponse(
                event_id=request.event_id,
                session_id=request.session_id,
                accepted=recorded.accepted,
                duplicate=recorded.duplicate,
                route_required=True,
            )
        if not trace_complete:
            response.headers["X-OIR-Trace-Completeness"] = "incomplete"
        return AgentEventCompatResponse(
            event_id=request.event_id,
            session_id=request.session_id,
            accepted=True,
            duplicate=result.duplicate if result else False,
            route_required=True,
        )
    except (ExecutionTicketError, ValueError) as exc:
        if conflict := await _plan_event_conflict_response(
            request,
            identity=identity,
            ports=ports,
        ):
            return conflict
        _raise_projected(exc)


@router.post("/plans/{plan_id}/confirm", response_model=PlanConfirmResponse)
async def confirm_plan(
    plan_id: str,
    request: PlanConfirmRequest,
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
        if ports.plan_preflight is None:
            raise PlanBindingUnavailableError(
                "Plan Binding is unavailable",
                details={"reason_code": "plan_preflight_unavailable"},
            )
        await ports.plan_preflight.preflight_plan(plan, user=_user(identity))
        response = await ports.plans.confirm(
            plan_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            request_id=request.request_id,
            expected_state_version=request.expected_state_version,
        )
        canonical = await ports.plans.get_plan(
            plan_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        if canonical is None:
            raise KeyError(plan_id)
        projected = plan_confirm_to_compat(response, plan=canonical)
        return projected.model_copy(update={"conflict": not response.transitioned})
    except Exception as exc:
        _raise_projected(exc)


def _user(identity: TrustedHostIdentity) -> UserContext:
    try:
        bundle = (
            OAC_BUNDLE_CATALOG.by_id[identity.active_bundle_id]
            if identity.active_bundle_id
            else None
        )
        entitlements = list(bundle.grants) if bundle else []
    except KeyError as exc:
        raise HostAuthenticationError from exc
    attributes = {"tenant_id": identity.tenant_id}
    if bundle is not None:
        # This attribute is created only after Host V2 signature and bundle
        # verification. The Knowledge issuer must never copy caller body tags.
        attributes["knowledge_access_tags"] = [bundle.legacy_tag]
    return UserContext(
        id=identity.user_id,
        roles=list(identity.roles),
        groups=list(identity.groups),
        entitlements=entitlements,
        attributes=attributes,
    )


def _authorize(identity: TrustedHostIdentity, operation: HostOperation) -> None:
    try:
        authorize_host_operation(identity, operation)
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc


def _raise_projected(error: Exception) -> None:
    status_code, body = project_error(error)
    raise HTTPException(status_code=status_code, detail=body.model_dump(mode="json")) from error


async def _record_trace(
    ports: OacAdapterApplicationPorts,
    event: ExecutionTraceEventDraft,
) -> bool:
    if ports.execution_traces is None:
        return True
    try:
        try_record = getattr(ports.execution_traces, "try_record", None)
        if callable(try_record):
            return await try_record(event)
        await ports.execution_traces.record(event)
    except Exception:
        return False
    return True


async def _record_context_and_recall_trace(
    ports: OacAdapterApplicationPorts,
    *,
    response,
    identity: TrustedHostIdentity,
    turn_id: str,
) -> bool:
    metadata = response.context.metadata
    pack = metadata.get("context_pack")
    if not isinstance(pack, dict):
        return True

    usage = pack.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    consumer = _bounded_trace_string(pack.get("consumer"), fallback="router")
    included_count = _bounded_trace_count(usage.get("included_count"))
    excluded_count = _bounded_trace_count(usage.get("dropped_count"))
    reason_code = _context_trace_reason_code(pack)
    degraded = reason_code is not None
    trace_id = trace_id_for_turn(turn_id)

    complete = await _record_trace(
        ports,
        ExecutionTraceEventDraft(
            trace_id=trace_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            session_id=response.session_id,
            turn_id=turn_id,
            event_type="context_pack",
            stage="context_ready",
            status="degraded" if degraded else "completed",
            source="oir:context_pack",
            source_event_id=f"context-pack:{response.request_id}",
            facts={
                "consumer": consumer,
                "included_count": included_count,
                "excluded_count": excluded_count,
                "degraded": degraded,
                "reason_code": reason_code,
            },
        ),
    )

    for scope, counts in _memory_recall_counts(pack).items():
        complete = (
            await _record_trace(
                ports,
                ExecutionTraceEventDraft(
                    trace_id=trace_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    session_id=response.session_id,
                    turn_id=turn_id,
                    event_type="memory_recall",
                    stage="recall_ready",
                    status="degraded" if degraded else "completed",
                    source="oir:memory_recall",
                    source_event_id=f"memory-recall:{response.request_id}:{scope}",
                    facts={
                        "scope": scope,
                        "used_count": counts["included"],
                        "excluded_count": counts["excluded"],
                        "degraded": degraded,
                        "reason_code": reason_code,
                    },
                ),
            )
            and complete
        )
    return complete


def _memory_recall_counts(pack: dict) -> dict[str, dict[str, int]]:
    selection = pack.get("selection")
    if not isinstance(selection, list):
        return {}
    counts: dict[str, dict[str, int]] = {}
    for item in selection:
        if not isinstance(item, dict) or item.get("source") != "memory":
            continue
        scope = _bounded_trace_string(item.get("scope"), fallback="unspecified")
        values = counts.setdefault(scope, {"included": 0, "excluded": 0})
        if item.get("included") is True:
            values["included"] += 1
        else:
            values["excluded"] += 1
    return counts


def _context_trace_reason_code(pack: dict) -> str | None:
    outcomes = pack.get("provider_outcomes")
    if not isinstance(outcomes, list):
        return None
    statuses = {
        str(item.get("status"))
        for item in outcomes
        if isinstance(item, dict) and item.get("status") in {"denied", "timeout", "error"}
    }
    if not statuses:
        return None
    return f"context_{sorted(statuses)[0]}"


def _bounded_trace_count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _bounded_trace_string(value, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    return normalized[:128] if normalized else fallback


def _trace_progress_facts(request: AgentEventRequest) -> dict[str, str]:
    facts = {"agent_id": request.agent_id}
    if not isinstance(request.output, dict):
        return facts
    stage_name = _bounded_optional_trace_string(request.output.get("provider_stage_name"))
    if stage_name is not None:
        facts["provider_stage_name"] = stage_name
    return facts


def _trace_failure_facts(request: AgentEventRequest) -> dict[str, str]:
    return {
        "agent_id": request.agent_id,
        "error_code": _safe_provider_error_code(request.output),
    }


def _failure_command_error(request: AgentEventRequest) -> dict[str, str]:
    return {"code": _safe_provider_error_code(request.output)}


def _safe_provider_error_code(output) -> str:
    if isinstance(output, dict):
        code = output.get("error_code")
        if code in {
            "workflow_empty_result",
            "workflow_execution_failed",
            "workflow_request_failed",
            "workflow_unavailable",
        }:
            return code
    return "provider_execution_failed"


def _bounded_optional_trace_string(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:128] or None
