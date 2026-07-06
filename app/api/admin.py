from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_admin_token
from app.dependencies import get_registry_service
from app.schemas.agents import AgentDefinition, AgentEnabledRequest, AgentListResponse, AgentPublic
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
