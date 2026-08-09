from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import MemoryActor, optional_memory_actor, require_memory_actor
from app.dependencies import (
    get_memory_management_service,
    get_memory_observability_service,
    get_memory_service,
)
from app.schemas.memory import (
    MemoryCleanupResult,
    MemoryDebugResponse,
    MemoryDeleteRequest,
    MemoryManagementOperationResponse,
    MemoryPendingDecisionActionRequest,
    MemoryRecallRequest,
    MemoryRecallResponse,
    MemoryWriteCandidate,
    MemoryWriteDecision,
)
from app.services.memory_management import (
    MemoryManagementConflict,
    MemoryManagementNotFound,
    MemoryManagementService,
)
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


@router.post("/recall", response_model=MemoryRecallResponse)
async def recall_memory(
    payload: MemoryRecallRequest,
    service: MemoryService = Depends(get_memory_service),
) -> MemoryRecallResponse:
    return await service.recall(payload)


@router.post("/write-candidates", response_model=list[MemoryWriteDecision])
async def write_memory_candidates(
    payload: list[MemoryWriteCandidate],
    user_id: str,
    tenant_id: str | None = None,
    service: MemoryService = Depends(get_memory_service),
) -> list[MemoryWriteDecision]:
    return await service.write_candidates(candidates=payload, user_id=user_id, tenant_id=tenant_id)


@router.post("/cleanup", response_model=MemoryCleanupResult)
async def cleanup_memory(
    service: MemoryService = Depends(get_memory_service),
) -> MemoryCleanupResult:
    return await service.cleanup_expired()


@router.get("/debug", response_model=MemoryDebugResponse)
async def memory_debug(
    user_id: str | None = None,
    tenant_id: str | None = None,
    agent_id: str | None = None,
    scopes: str | None = None,
    memory_id: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    run_id: str | None = None,
    formation_job_id: str | None = None,
    memory_key: str | None = None,
    decision_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    actor: MemoryActor | None = Depends(optional_memory_actor),
    service: MemoryService = Depends(get_memory_service),
    observability: MemoryObservabilityService = Depends(get_memory_observability_service),
) -> MemoryDebugResponse:
    parsed_scopes = [item.strip() for item in scopes.split(",") if item.strip()] if scopes else None
    if actor is None and not tenant_id:
        tenant_scoped_filters = (
            memory_id,
            request_id,
            session_id,
            turn_id,
            run_id,
            formation_job_id,
            memory_key,
            decision_status,
        )
        if any(tenant_scoped_filters):
            raise HTTPException(
                status_code=400,
                detail="tenant_id is required for formation and lifecycle debug filters",
            )
        return await service.debug_state(
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            scopes=parsed_scopes,
            limit=limit,
        )
    if actor is not None and (
        (tenant_id is not None and tenant_id != actor.tenant_id)
        or (user_id is not None and user_id != actor.user_id)
    ):
        raise HTTPException(status_code=404, detail="Memory debug target not found")
    resolved_tenant = actor.tenant_id if actor is not None else tenant_id
    resolved_user = actor.user_id if actor is not None else user_id
    if not resolved_tenant:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    return await observability.debug_state(
        user_id=resolved_user,
        tenant_id=resolved_tenant,
        agent_id=agent_id,
        scopes=parsed_scopes,
        memory_id=memory_id,
        request_id=request_id,
        session_id=session_id,
        turn_id=turn_id,
        run_id=run_id,
        formation_job_id=formation_job_id,
        memory_key=memory_key,
        decision_status=decision_status,
        limit=limit,
    )


@router.delete("/{memory_id}", response_model=MemoryManagementOperationResponse)
async def delete_owned_memory(
    memory_id: str,
    payload: MemoryDeleteRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    service: MemoryManagementService = Depends(get_memory_management_service),
) -> MemoryManagementOperationResponse:
    return await _management_call(
        service.request_delete(
            memory_id=memory_id,
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            actor=actor.user_id,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            expected_revision_id=payload.expected_revision_id,
        )
    )


@router.post("/pending/{decision_id}/confirm", response_model=MemoryManagementOperationResponse)
async def confirm_pending_memory(
    decision_id: str,
    payload: MemoryPendingDecisionActionRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    service: MemoryManagementService = Depends(get_memory_management_service),
) -> MemoryManagementOperationResponse:
    return await _management_call(
        service.resolve_pending(
            decision_id=decision_id,
            action="confirm",
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            actor=actor.user_id,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            expected_revision_id=payload.expected_revision_id,
        )
    )


@router.post("/pending/{decision_id}/reject", response_model=MemoryManagementOperationResponse)
async def reject_pending_memory(
    decision_id: str,
    payload: MemoryPendingDecisionActionRequest,
    actor: MemoryActor = Depends(require_memory_actor),
    service: MemoryManagementService = Depends(get_memory_management_service),
) -> MemoryManagementOperationResponse:
    return await _management_call(
        service.resolve_pending(
            decision_id=decision_id,
            action="reject",
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            actor=actor.user_id,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            expected_revision_id=payload.expected_revision_id,
        )
    )


@router.get("/operations/{index_operation_id}", response_model=MemoryManagementOperationResponse)
async def memory_operation_status(
    index_operation_id: str,
    actor: MemoryActor = Depends(require_memory_actor),
    service: MemoryManagementService = Depends(get_memory_management_service),
) -> MemoryManagementOperationResponse:
    return await _management_call(
        service.operation_status(
            index_operation_id=index_operation_id,
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
        )
    )


async def _management_call(awaitable):
    try:
        return await awaitable
    except MemoryManagementNotFound as exc:
        raise HTTPException(status_code=404, detail="Memory operation target not found") from exc
    except MemoryManagementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
