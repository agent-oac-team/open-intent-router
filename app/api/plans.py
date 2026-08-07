from fastapi import APIRouter, Depends, HTTPException

from app.core.security import bind_principal_user_context, require_native_principal
from app.dependencies import get_plan_executor, get_plan_service
from app.schemas.plans import (
    Plan,
    PlanActionRequest,
    PlanActionResponse,
    PlanExecutionRequest,
    PlanExecutionResponse,
)
from app.schemas.security import NativePrincipal
from app.services.plan_executor import PlanExecutor, plan_execution_prohibits_memory
from app.services.plan_service import PlanService

router = APIRouter(prefix="/api/v1", tags=["plans"])


@router.get("/plans/{plan_id}", response_model=Plan)
async def get_plan(
    plan_id: str,
    principal: NativePrincipal = Depends(require_native_principal),
    plan_service: PlanService = Depends(get_plan_service),
) -> Plan:
    plan = await plan_service.get_plan(
        plan_id,
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.post("/plans/{plan_id}/actions", response_model=PlanActionResponse)
async def plan_action(
    plan_id: str,
    payload: PlanActionRequest,
    principal: NativePrincipal = Depends(require_native_principal),
    plan_service: PlanService = Depends(get_plan_service),
    executor: PlanExecutor = Depends(get_plan_executor),
) -> PlanActionResponse:
    try:
        user = bind_principal_user_context(payload.user, principal)
        if payload.action == "confirm":
            await executor.preflight(plan_id, user=user)
            return await plan_service.confirm(
                plan_id,
                tenant_id=principal.tenant_id,
                user_id=principal.subject,
            )
        return await plan_service.cancel(
            plan_id,
            tenant_id=principal.tenant_id,
            user_id=principal.subject,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/execute", response_model=PlanExecutionResponse)
async def execute_plan(
    plan_id: str,
    payload: PlanExecutionRequest,
    principal: NativePrincipal = Depends(require_native_principal),
    executor: PlanExecutor = Depends(get_plan_executor),
) -> PlanExecutionResponse:
    try:
        return await executor.execute(
            plan_id,
            user=bind_principal_user_context(payload.user, principal),
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
    principal: NativePrincipal = Depends(require_native_principal),
    plan_service: PlanService = Depends(get_plan_service),
    executor: PlanExecutor = Depends(get_plan_executor),
) -> PlanExecutionResponse:
    try:
        user = bind_principal_user_context(payload.user, principal)
        selected_definitions = await executor.preflight(plan_id, user=user)
        await plan_service.confirm(
            plan_id,
            tenant_id=principal.tenant_id,
            user_id=principal.subject,
            publish=not plan_execution_prohibits_memory(payload.input, payload.context),
        )
        return await executor.execute(
            plan_id,
            user=user,
            input_values=payload.input,
            context=payload.context,
            max_steps=payload.max_steps,
            selected_definitions=selected_definitions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/plans/{plan_id}/resume", response_model=PlanExecutionResponse)
async def resume_plan(
    plan_id: str,
    payload: PlanExecutionRequest,
    principal: NativePrincipal = Depends(require_native_principal),
    executor: PlanExecutor = Depends(get_plan_executor),
) -> PlanExecutionResponse:
    try:
        return await executor.execute(
            plan_id,
            user=bind_principal_user_context(payload.user, principal),
            input_values=payload.input,
            context=payload.context,
            max_steps=payload.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
