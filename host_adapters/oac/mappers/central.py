from app.core.errors import AppError
from app.schemas.common import ArtifactRef, UserContext
from app.schemas.events import AgentEvent, ConversationEvent
from app.schemas.plans import Plan, PlanActionResponse
from app.schemas.routing import CurrentAgentContext, InputPayload, RouteRequest, RouteResponse
from host_adapters.oac.schemas.central import (
    AgentEventRequest,
    CentralRouteRequest,
    CentralRouteResponse,
    CompatErrorResponse,
    LegacyPlan,
    LegacyPlanStep,
    LegacyRoute,
    LegacyRouteContext,
    NavigationEventRequest,
    PlanConfirmResponse,
)

SOURCE_TO_NATIVE = {
    "central_chat": "host_chat",
    "agent_chat": "agent_chat",
    "agent_event": "agent_event",
    "plan_control": "plan_control",
}
SOURCE_TO_LEGACY = {value: key for key, value in SOURCE_TO_NATIVE.items()}
_SAFE_REASON_CODES = frozenset(
    {
        "binding_requirement_invalid",
        "binding_resolver_unavailable",
        "binding_snapshot_invalid",
        "binding_unavailable",
        "execution_ticket_compensation_failed",
        "execution_ticket_unavailable",
        "external_execution_binding_invalid",
        "external_execution_handling_invalid",
        "external_execution_unavailable",
        "external_executor_unauthorized",
        "external_executor_unhealthy",
        "external_executor_unsupported",
        "invocation_adapter_incompatible",
        "invocation_adapter_missing",
        "invocation_adapter_unsupported",
        "invocation_application_unavailable",
        "invocation_config_invalid",
        "plan_binding_agent_mismatch",
        "plan_binding_incomplete",
        "plan_binding_requirement_incompatible",
        "plan_binding_revision_incompatible",
        "plan_binding_unavailable",
        "plan_preflight_unavailable",
        "plan_snapshot_unavailable",
        "route_binding_unavailable",
    }
)


def route_request_to_native(request: CentralRouteRequest, *, user: UserContext) -> RouteRequest:
    history = [
        item.model_dump(mode="json")
        for item in (*request.central_chat_history, *request.agent_chat_history)
    ]
    frontend_context = dict(request.frontend_context)
    if history:
        frontend_context["conversation_history"] = history
    current_agent = None
    if request.current_agent_id:
        current_agent = CurrentAgentContext(
            agent_id=request.current_agent_id,
            agent_session_id=request.current_agent_session_id,
        )
    text = request.user_query or _control_input(request)
    return RouteRequest(
        request_id=request.request_id,
        session_id=request.session_id,
        source=SOURCE_TO_NATIVE[request.source],
        user=user,
        input=InputPayload(text=text),
        current_agent=current_agent,
        event_id=request.event_id,
        plan_id=request.plan_id,
        step_id=request.step_id,
        frontend_context=frontend_context,
    )


def route_response_to_compat(
    response: RouteResponse,
    *,
    source: str,
    execution_ticket: str | None = None,
) -> CentralRouteResponse:
    message = response.assistant_message or response.decision.message
    return CentralRouteResponse(
        request_id=response.request_id,
        session_id=response.session_id,
        route=LegacyRoute(
            status=response.decision.status,
            action=response.decision.action,
            agent_id=response.decision.target_agent_id,
            message=message,
        ),
        context=LegacyRouteContext(
            source=source,
            current_agent_id=response.context.current_agent_id,
            relation=response.context.relation,
            artifact_refs=[_artifact_id(item) for item in response.context.artifact_refs],
        ),
        plan=plan_to_compat(response.plan) if response.plan else None,
        next_action=_next_action_to_compat(
            response.next_action or (response.plan.next_action if response.plan else None)
        ),
        execution_ticket=execution_ticket,
    )


def navigation_event_to_native(
    request: NavigationEventRequest, *, user: UserContext, event_id: str
) -> ConversationEvent:
    return ConversationEvent(
        event_id=event_id,
        session_id=request.session_id,
        user_id=user.id,
        event_type=request.event_type,
        source="system",
        agent_id=request.current_agent_id,
        payload={
            "from": request.from_path,
            "to": request.to,
            "reason": request.reason,
            "current_agent_session_id": request.current_agent_session_id,
        },
        created_at=request.created_at,
    )


def agent_event_to_native(
    request: AgentEventRequest,
    *,
    user: UserContext,
    run_id: str | None,
    turn_id: str | None,
    request_id: str | None,
) -> AgentEvent:
    output = request.output if isinstance(request.output, dict) else {"value": request.output}
    return AgentEvent(
        event_id=request.event_id,
        run_id=run_id,
        request_id=request_id,
        session_id=request.session_id,
        agent_id=request.agent_id,
        user_id=user.id,
        tenant_id=user.tenant_id,
        turn_id=turn_id,
        agent_session_id=request.agent_session_id,
        event_type=request.event_type,
        status=request.status,
        plan_id=request.plan_id,
        step_id=request.step_id,
        payload={
            "message": request.message,
            "output": output,
            "artifact_refs": request.artifact_refs,
            "result_ref": request.result_ref,
        },
        created_at=request.created_at,
    )


def plan_confirm_to_compat(response: PlanActionResponse, *, plan: Plan) -> PlanConfirmResponse:
    step = next((item for item in plan.steps if item.step_id == response.current_step_id), None)
    return PlanConfirmResponse(
        plan_id=response.plan_id,
        session_id=plan.session_id or "",
        status=response.status,
        current_step_id=response.current_step_id,
        current_step=(
            {
                "step_id": step.step_id,
                "agent_id": step.agent_id,
                "status": step.status,
                "description": step.description,
                "runtime_status": response.status,
            }
            if step
            else None
        ),
        state_version=plan.state_version,
        next_action=_next_action_to_compat(plan.next_action),
    )


def project_error(error: Exception) -> tuple[int, CompatErrorResponse]:
    if isinstance(error, AppError):
        return error.status_code, CompatErrorResponse(
            code=error.code,
            message=error.message,
            details=_safe_error_details(error.details),
        )
    if isinstance(error, PermissionError):
        return 403, CompatErrorResponse(code="forbidden", message="Operation is not allowed")
    if isinstance(error, KeyError):
        return 404, CompatErrorResponse(code="not_found", message="Resource was not found")
    if isinstance(error, ValueError):
        return 409, CompatErrorResponse(code="conflict", message=str(error) or "Conflict")
    return 500, CompatErrorResponse(code="internal_error", message="Internal server error")


def _safe_error_details(details: object) -> dict[str, str]:
    """Expose only the bounded machine-readable reason used by compatibility clients."""

    if not isinstance(details, dict):
        return {}
    reason_code = details.get("reason_code")
    if isinstance(reason_code, str) and reason_code in _SAFE_REASON_CODES:
        return {"reason_code": reason_code}
    return {}


def _control_input(request: CentralRouteRequest) -> str:
    if request.plan_action:
        return f"plan:{request.plan_action}"
    if request.event_id:
        return f"event:{request.event_id}"
    return "continue"


def _artifact_id(item: ArtifactRef) -> str:
    return item.artifact_id


def plan_to_compat(plan: Plan) -> LegacyPlan:
    current = plan.current_step_id or (plan.steps[0].step_id if plan.steps else "")
    return LegacyPlan(
        plan_id=plan.plan_id,
        current_step=current,
        status=plan.status,
        state_version=plan.state_version,
        next_action=_next_action_to_compat(plan.next_action),
        steps=[
            LegacyPlanStep(
                step_id=step.step_id,
                agent_id=step.agent_id,
                status=step.status if step.status != "cancelled" else "failed",
                description=step.description,
            )
            for step in plan.steps
        ],
    )


def _next_action_to_compat(next_action):
    return next_action.model_dump(mode="json") if next_action is not None else None
