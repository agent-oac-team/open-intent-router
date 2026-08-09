from fastapi import APIRouter, Depends, Header, HTTPException

from app.dependencies import get_native_agent_event_service
from app.schemas.events import AgentEvent, AgentEventResponse
from app.services.agent_event_service import AgentEventRejected, NativeAgentEventService

router = APIRouter(prefix="/api/v1", tags=["events"])

EXECUTION_TICKET_HEADER = "X-OIR-Execution-Ticket"

_REJECTION_STATUS = {
    "unauthorized": 401,
    "conflict": 409,
    "invalid": 422,
    "not_found": 404,
}


@router.post("/events/agent", response_model=AgentEventResponse)
async def agent_event(
    payload: AgentEvent,
    execution_ticket: str | None = Header(default=None, alias=EXECUTION_TICKET_HEADER),
    service: NativeAgentEventService = Depends(get_native_agent_event_service),
) -> AgentEventResponse:
    return await _record_agent_event(
        service,
        payload,
        execution_ticket=execution_ticket,
    )


@router.post("/runs/{run_id}/events", response_model=AgentEventResponse)
async def run_agent_event(
    run_id: str,
    payload: AgentEvent,
    execution_ticket: str | None = Header(default=None, alias=EXECUTION_TICKET_HEADER),
    service: NativeAgentEventService = Depends(get_native_agent_event_service),
) -> AgentEventResponse:
    return await _record_agent_event(
        service,
        payload,
        execution_ticket=execution_ticket,
        path_run_id=run_id,
    )


async def _record_agent_event(
    service: NativeAgentEventService,
    payload: AgentEvent,
    *,
    execution_ticket: str | None,
    path_run_id: str | None = None,
) -> AgentEventResponse:
    try:
        return await service.record(
            payload,
            execution_ticket=execution_ticket,
            path_run_id=path_run_id,
        )
    except AgentEventRejected as exc:
        raise HTTPException(
            status_code=_REJECTION_STATUS[exc.category],
            detail=exc.detail,
        ) from exc
