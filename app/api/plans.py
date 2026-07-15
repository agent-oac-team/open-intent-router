from fastapi import APIRouter, Depends, HTTPException

from app.core.security import MemoryActor, bind_trusted_user_context, require_memory_actor
from app.dependencies import get_plan_executor, get_plan_service
from app.schemas.plans import (
    Plan,
    PlanActionRequest,
    PlanActionResponse,
    PlanExecutionRequest,
    PlanExecutionResponse,
)
from app.services.plan_executor import PlanExecutor, plan_execution_prohibits_memory
from app.services.plan_service import PlanService

router = APIRouter(prefix="/api/v1", tags=["plans"])


@router.get("/plans/{plan_id}", response_model=Plan)
async def get_plan(
    plan_id: str,
    actor: MemoryActor = Depends(require_memory_actor),
    plan_service: PlanService = Depends(get_plan_service),
) -> Plan:
    plan = await plan_service.get_plan(
        plan_id,
        tenant_id=actor.tenant_id,
        user_id=actor.user_id,
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.post("/plans/{plan_id}/actions", response_model=PlanActionResponse)
async def plan_action(
    plan_id: str,
    payload: PlanActionRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    plan_service: PlanService = Depends(get_plan_service),
) -> PlanActionResponse:
    try:
        if payload.action == "confirm":
            return await plan_service.confirm(
                plan_id,
                tenant_id=actor.tenant_id,
                user_id=actor.user_id,
            )
        return await plan_service.cancel(
            plan_id,
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/execute", response_model=PlanExecutionResponse)
async def execute_plan(
    plan_id: str,
    payload: PlanExecutionRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    executor: PlanExecutor = Depends(get_plan_executor),
) -> PlanExecutionResponse:
    try:
        return await executor.execute(
            plan_id,
            user=bind_trusted_user_context(payload.user, actor),
            input_values=payload.input,
            context=payload.context,
            max_steps=payload.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/confirm-and-execute", response_model=PlanExecutionResponse)
async def confirm_and_execute_plan(
    plan_id: str,
    payload: PlanExecutionRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    plan_service: PlanService = Depends(get_plan_service),
    executor: PlanExecutor = Depends(get_plan_executor),
) -> PlanExecutionResponse:
    try:
        await plan_service.confirm(
            plan_id,
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            publish=not plan_execution_prohibits_memory(payload.input, payload.context),
        )
        return await executor.execute(
            plan_id,
            user=bind_trusted_user_context(payload.user, actor),
            input_values=payload.input,
            context=payload.context,
            max_steps=payload.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/resume", response_model=PlanExecutionResponse)
async def resume_plan(
    plan_id: str,
    payload: PlanExecutionRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    executor: PlanExecutor = Depends(get_plan_executor),
) -> PlanExecutionResponse:
    try:
        return await executor.execute(
            plan_id,
            user=bind_trusted_user_context(payload.user, actor),
            input_values=payload.input,
            context=payload.context,
            max_steps=payload.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
