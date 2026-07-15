from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_event_service, get_plan_service, get_run_repository
from app.schemas.events import AgentEvent, AgentEventResponse
from app.services.event_service import EventService
from app.services.plan_service import PlanService, validate_agent_event_contract

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.post("/events/agent", response_model=AgentEventResponse)
async def agent_event(
    payload: AgentEvent,
    event_service: EventService = Depends(get_event_service),
    plan_service: PlanService = Depends(get_plan_service),
    run_repository=Depends(get_run_repository),
) -> AgentEventResponse:
    _validate_event_contract(payload)
    plan = None
    formation_suppressed = False
    if payload.plan_id:
        plan, run = await _resolve_plan_event_target(
            payload, plan_service=plan_service, run_repository=run_repository
        )
        formation_suppressed = run.formation_suppressed
    payload = await _bind_event_owner(payload, plan=plan, run_repository=run_repository)
    response = await event_service.record_agent_event(payload)
    stored_event = await _stored_event_or_error(event_service, payload)
    if plan is not None:
        await plan_service.apply_agent_event(
            stored_event,
            tenant_id=plan.tenant_id,
            user_id=plan.user_id,
            publish=not formation_suppressed,
        )
    return response


@router.post("/runs/{run_id}/events", response_model=AgentEventResponse)
async def run_agent_event(
    run_id: str,
    payload: AgentEvent,
    event_service: EventService = Depends(get_event_service),
    plan_service: PlanService = Depends(get_plan_service),
    run_repository=Depends(get_run_repository),
) -> AgentEventResponse:
    event = payload.model_copy(update={"run_id": run_id})
    _validate_event_contract(event)
    plan = None
    formation_suppressed = False
    if event.plan_id:
        plan, run = await _resolve_plan_event_target(
            event, plan_service=plan_service, run_repository=run_repository
        )
        formation_suppressed = run.formation_suppressed
    event = await _bind_event_owner(event, plan=plan, run_repository=run_repository)
    response = await event_service.record_agent_event(event)
    stored_event = await _stored_event_or_error(event_service, event)
    if plan is not None:
        await plan_service.apply_agent_event(
            stored_event,
            tenant_id=plan.tenant_id,
            user_id=plan.user_id,
            publish=not formation_suppressed,
        )
    return response


async def _stored_event_or_error(event_service: EventService, incoming: AgentEvent) -> AgentEvent:
    stored = await event_service.get_event(
        incoming.event_id,
        tenant_id=incoming.tenant_id,
        user_id=incoming.user_id,
    )
    if stored is None:
        raise HTTPException(status_code=409, detail="Agent event identity conflict")
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
        raise HTTPException(status_code=409, detail="Agent event identity conflict")
    return stored


def _validate_event_contract(event: AgentEvent) -> None:
    try:
        validate_agent_event_contract(event)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _bind_event_owner(event: AgentEvent, *, plan, run_repository) -> AgentEvent:
    if plan is not None:
        return event.model_copy(update={"user_id": plan.user_id, "tenant_id": plan.tenant_id})
    if not event.run_id:
        return event.model_copy(update={"user_id": None, "tenant_id": None})
    run = await run_repository.get_run(event.run_id)
    if run is None or run.session_id != event.session_id or run.agent_id != event.agent_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return event.model_copy(update={"user_id": run.user_id, "tenant_id": run.tenant_id})


async def _resolve_plan_event_target(
    event: AgentEvent,
    *,
    plan_service: PlanService,
    run_repository,
):
    if not event.run_id:
        raise HTTPException(status_code=422, detail="Trusted Run association is required")
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
        raise HTTPException(status_code=404, detail="Plan not found")
    plan = await plan_service.get_plan(event.plan_id, tenant_id=run.tenant_id, user_id=run.user_id)
    if (
        plan is None
        or plan.session_id != event.session_id
        or not any(
            step.step_id == event.step_id and step.agent_id == event.agent_id for step in plan.steps
        )
    ):
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan, run
