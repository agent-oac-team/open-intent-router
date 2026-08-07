from fastapi import APIRouter, Depends, HTTPException

from app.core.security import bind_principal_user_context, require_native_principal
from app.dependencies import get_registry_service
from app.schemas.agents import (
    AgentListResponse,
    AgentPublic,
    AvailableAgentsRequest,
    AvailableAgentsResponse,
)
from app.schemas.security import NativePrincipal
from app.services.registry_service import AgentRegistryService

router = APIRouter(prefix="/api/v1", tags=["agents"])


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentListResponse:
    return await registry.list_public()


@router.get("/agents/{agent_id}", response_model=AgentPublic)
async def get_agent(
    agent_id: str,
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AgentPublic:
    agent = await registry.public_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("/agents/available", response_model=AvailableAgentsResponse)
async def available_agents(
    payload: AvailableAgentsRequest,
    principal: NativePrincipal = Depends(require_native_principal),
    registry: AgentRegistryService = Depends(get_registry_service),
) -> AvailableAgentsResponse:
    return await registry.available_for_user(bind_principal_user_context(payload.user, principal))
