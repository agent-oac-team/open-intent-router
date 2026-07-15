from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import AdminActor, require_admin_token
from app.dependencies import (
    get_memory_management_service,
    get_memory_observability_service,
    get_registry_service,
)
from app.schemas.agents import AgentDefinition, AgentEnabledRequest, AgentListResponse, AgentPublic
from app.schemas.memory import (
    MemoryAdminActionRequest,
    MemoryDebugResponse,
    MemoryManagementOperationResponse,
    MemoryMetricsResponse,
    MemoryRuntimeHealth,
)
from app.services.memory_management import (
    MemoryManagementConflict,
    MemoryManagementNotFound,
    MemoryManagementService,
)
from app.services.memory_observability import MemoryObservabilityService
from app.services.registry_service import AgentRegistryService

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get(
    "/agents", response_model=AgentListResponse, dependencies=[Depends(require_admin_token)]
)
async def admin_list_agents(
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentListResponse:
    return await registry.list_public()


@router.post("/agents", response_model=AgentDefinition, dependencies=[Depends(require_admin_token)])
async def upsert_agent(
    payload: AgentDefinition,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentDefinition:
    return await registry.upsert_definition(payload)


@router.put(
    "/agents/{agent_id}",
    response_model=AgentDefinition,
    dependencies=[Depends(require_admin_token)],
)
async def update_agent(
    agent_id: str,
    payload: AgentDefinition,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentDefinition:
    if payload.agent_id != agent_id:
        raise HTTPException(status_code=400, detail="agent_id in path and payload must match")
    return await registry.upsert_definition(payload)


@router.patch(
    "/agents/{agent_id}/enabled",
    response_model=AgentPublic,
    dependencies=[Depends(require_admin_token)],
)
async def set_agent_enabled(
    agent_id: str,
    payload: AgentEnabledRequest,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentPublic:
    updated = await registry.set_enabled(agent_id, payload.enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return updated.to_public()


@router.delete("/agents/{agent_id}", dependencies=[Depends(require_admin_token)])
async def delete_agent(
    agent_id: str,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> dict[str, bool]:
    deleted = await registry.delete_definition(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"deleted": True}


@router.post("/registry/reload", dependencies=[Depends(require_admin_token)])
async def reload_registry(
    registry: AgentRegistryService = Depends(get_registry_service),
) -> dict[str, str]:
    state = await registry.reload()
    return {
        "status": state.status,
        "active_source": state.active_source,
        "message": state.message,
    }


@router.get(
    "/memories/debug",
    response_model=MemoryDebugResponse,
    dependencies=[Depends(require_admin_token)],
)
async def admin_memory_debug(
    tenant_id: str,
    user_id: str | None = None,
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
    service: MemoryObservabilityService = Depends(get_memory_observability_service),
) -> MemoryDebugResponse:
    parsed_scopes = [item.strip() for item in scopes.split(",") if item.strip()] if scopes else None
    return await service.debug_state(
        tenant_id=tenant_id,
        user_id=user_id,
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


@router.post(
    "/memories/{memory_id}/delete",
    response_model=MemoryManagementOperationResponse,
)
async def admin_delete_memory(
    memory_id: str,
    payload: MemoryAdminActionRequest,
    admin_actor: AdminActor = Depends(require_admin_token),
    service: MemoryManagementService = Depends(get_memory_management_service),
) -> MemoryManagementOperationResponse:
    return await _admin_management_call(
        service.request_delete(
            memory_id=memory_id,
            tenant_id=payload.tenant_id,
            user_id=payload.user_id,
            actor=admin_actor.actor_id,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            expected_revision_id=payload.expected_revision_id,
            admin=True,
        )
    )


@router.post(
    "/memories/pending/{decision_id}/{action}",
    response_model=MemoryManagementOperationResponse,
)
async def admin_resolve_pending_memory(
    decision_id: str,
    action: str,
    payload: MemoryAdminActionRequest,
    admin_actor: AdminActor = Depends(require_admin_token),
    service: MemoryManagementService = Depends(get_memory_management_service),
) -> MemoryManagementOperationResponse:
    if action not in {"confirm", "reject"}:
        raise HTTPException(status_code=404, detail="Memory operation target not found")
    return await _admin_management_call(
        service.resolve_pending(
            decision_id=decision_id,
            action=action,
            tenant_id=payload.tenant_id,
            user_id=payload.user_id,
            actor=admin_actor.actor_id,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            expected_revision_id=payload.expected_revision_id,
            admin=True,
        )
    )


@router.get(
    "/memories/operations/{index_operation_id}",
    response_model=MemoryManagementOperationResponse,
    dependencies=[Depends(require_admin_token)],
)
async def admin_memory_operation_status(
    index_operation_id: str,
    tenant_id: str,
    user_id: str,
    service: MemoryManagementService = Depends(get_memory_management_service),
) -> MemoryManagementOperationResponse:
    return await _admin_management_call(
        service.operation_status(
            index_operation_id=index_operation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            admin=True,
        )
    )


@router.get(
    "/memories/health",
    response_model=MemoryRuntimeHealth,
    dependencies=[Depends(require_admin_token)],
)
async def admin_memory_health(
    service: MemoryObservabilityService = Depends(get_memory_observability_service),
) -> MemoryRuntimeHealth:
    return await service.health()


@router.get(
    "/memories/metrics",
    response_model=MemoryMetricsResponse,
    dependencies=[Depends(require_admin_token)],
)
async def admin_memory_metrics(
    service: MemoryObservabilityService = Depends(get_memory_observability_service),
) -> MemoryMetricsResponse:
    return await service.metrics()


async def _admin_management_call(awaitable):
    try:
        return await awaitable
    except MemoryManagementNotFound as exc:
        raise HTTPException(status_code=404, detail="Memory operation target not found") from exc
    except MemoryManagementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
