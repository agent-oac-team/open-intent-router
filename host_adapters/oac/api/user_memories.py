from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas.memory import UserMemoryListResponse
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity import authorize_host_operation
from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

router = APIRouter(prefix="/api/v1/user-memories", tags=["user-memories"])


@router.get("", response_model=UserMemoryListResponse)
async def list_user_memories(
    memory_type: Literal["user_preference", "stable_fact"] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> UserMemoryListResponse:
    try:
        authorize_host_operation(identity, "read_only")
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc
    if ports.memory_management is None:
        raise HTTPException(status_code=503, detail="memory_management_unavailable")
    return await ports.memory_management.list_user_memories(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        memory_type=memory_type,
        page=page,
    )
