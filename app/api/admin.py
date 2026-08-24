from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.errors import RegistryUnavailableError
from app.core.security import AdminActor, require_admin_token
from app.dependencies import (
    get_memory_governance_service,
    get_memory_management_service,
    get_memory_observability_service,
    get_registry_service,
)
from app.schemas.agents import (
    AgentAdminListResponse,
    AgentDefinitionV2,
    AgentEnabledRequest,
    AgentPublicV2,
)
from app.schemas.memory import (
    MemoryAdminActionRequest,
    MemoryDebugResponse,
    MemoryGovernanceResponse,
    MemoryManagementOperationResponse,
    MemoryMetricsResponse,
    MemoryRuntimeHealth,
)
from app.schemas.runtime import (
    RuntimeInventoryDefinition,
    RuntimeInventoryQuarantine,
    RuntimeInventoryResponse,
)
from app.services.memory_governance import MemoryGovernanceService
from app.services.memory_management import (
    MemoryManagementConflict,
    MemoryManagementNotFound,
    MemoryManagementService,
)
from app.services.memory_observability import MemoryObservabilityService
from app.services.registry_service import AgentRegistryService
from app.services.runtime_readiness import RuntimeReadinessRuntime

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get(
    "/agents", response_model=AgentAdminListResponse, dependencies=[Depends(require_admin_token)]
)
async def admin_list_agents(
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentAdminListResponse:
    return await registry.list_admin()


@router.post(
    "/agents", response_model=AgentDefinitionV2, dependencies=[Depends(require_admin_token)]
)
async def upsert_agent(
    payload: AgentDefinitionV2,
    request: Request,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentDefinitionV2:
    updated = await registry.upsert_definition(payload)
    await _refresh_runtime_snapshot(request, registry)
    return updated


@router.put(
    "/agents/{agent_id}",
    response_model=AgentDefinitionV2,
    dependencies=[Depends(require_admin_token)],
)
async def update_agent(
    agent_id: str,
    payload: AgentDefinitionV2,
    request: Request,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentDefinitionV2:
    if payload.agent_id != agent_id:
        raise HTTPException(status_code=400, detail="agent_id in path and payload must match")
    updated = await registry.upsert_definition(payload)
    await _refresh_runtime_snapshot(request, registry)
    return updated


@router.patch(
    "/agents/{agent_id}/enabled",
    response_model=AgentPublicV2,
    dependencies=[Depends(require_admin_token)],
)
async def set_agent_enabled(
    agent_id: str,
    payload: AgentEnabledRequest,
    request: Request,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentPublicV2:
    updated = await registry.set_enabled(agent_id, payload.enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await _refresh_runtime_snapshot(request, registry)
    return updated.to_public()


@router.delete("/agents/{agent_id}", dependencies=[Depends(require_admin_token)])
async def delete_agent(
    agent_id: str,
    request: Request,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> dict[str, bool]:
    deleted = await registry.delete_definition(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found")
    await _refresh_runtime_snapshot(request, registry)
    return {"deleted": True}


@router.post("/registry/reload", dependencies=[Depends(require_admin_token)])
async def reload_registry(
    request: Request,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> dict[str, str]:
    readiness_runtime = getattr(request.app.state, "runtime_readiness_runtime", None)
    if isinstance(readiness_runtime, RuntimeReadinessRuntime):
        state = await readiness_runtime.reload_primary_registry(registry)
        if state is None:
            raise RegistryUnavailableError("Registry Snapshot is unavailable")
    else:
        state = await registry.reload()
    return {
        "status": state.status,
        "active_source": state.active_source,
        "message": "",
    }


async def _refresh_runtime_snapshot(request: Request, registry: AgentRegistryService) -> None:
    """Keep the app-owned Snapshot coherent after a committed Native Registry write."""

    readiness_runtime = getattr(request.app.state, "runtime_readiness_runtime", None)
    if not isinstance(readiness_runtime, RuntimeReadinessRuntime):
        return
    if not await readiness_runtime.refresh_registry_snapshot(registry):
        raise RegistryUnavailableError("Registry Snapshot is unavailable")


@router.get(
    "/runtime/inventory",
    response_model=RuntimeInventoryResponse,
    dependencies=[Depends(require_admin_token)],
)
async def runtime_inventory(request: Request) -> RuntimeInventoryResponse:
    readiness_runtime = getattr(request.app.state, "runtime_readiness_runtime", None)
    if not isinstance(readiness_runtime, RuntimeReadinessRuntime):
        return RuntimeInventoryResponse(
            status="error",
            runtime_status="error",
            registry_status="error",
            reason_code="core_runtime_unavailable",
        )
    report = await readiness_runtime.refresh()
    snapshot_runtime = getattr(request.app.state, "registry_snapshot_runtime", None)
    snapshot_status = snapshot_runtime.status if snapshot_runtime is not None else None
    return RuntimeInventoryResponse(
        status=report.status,
        runtime_status=report.runtime_status,
        registry_status=report.registry_status,
        active_source=report.active_source,
        reason_code=report.reason_code,
        impacted_definition_count=report.impacted_definition_count,
        quarantined_definition_count=(
            snapshot_status.quarantined_definition_count if snapshot_status is not None else 0
        ),
        quarantined_definitions=[
            RuntimeInventoryQuarantine(
                source_index=entry.source_index,
                agent_id=entry.agent_id,
                reason_code=entry.reason_code,
            )
            for entry in readiness_runtime.admin_quarantine_inventory()
        ],
        definitions=[
            RuntimeInventoryDefinition(
                agent_id=entry.agent_id,
                revision=entry.revision,
                enabled=entry.enabled,
                handling_kind=entry.handling_kind,
                handling=dict(entry.handling),
                binding_status=entry.binding_status,
                isolation_reason_code=entry.isolation_reason_code,
            )
            for entry in readiness_runtime.admin_inventory()
        ],
    )


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


@router.get(
    "/memories/governance",
    response_model=MemoryGovernanceResponse,
    dependencies=[Depends(require_admin_token)],
)
async def admin_memory_governance(
    tenant_id: str,
    memory_id: str | None = None,
    status: Literal["needs_attention", "blocked", "repairing"] | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: MemoryGovernanceService = Depends(get_memory_governance_service),
) -> MemoryGovernanceResponse:
    return await service.query(
        tenant_id=tenant_id,
        memory_id=memory_id,
        status=status,
        page=page,
        page_size=page_size,
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
