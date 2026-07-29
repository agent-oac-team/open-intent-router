from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas.memory import (
    UserMemoryDeleteRequest,
    UserMemoryDeleteResponse,
    UserMemoryListResponse,
)
from app.services.memory_management import MemoryManagementConflict, MemoryManagementNotFound
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
    if identity.credential_class != "oac_user":
        raise HTTPException(status_code=403, detail="host_operation_forbidden")
    if ports.memory_management is None:
        raise HTTPException(status_code=503, detail="memory_management_unavailable")
    return await ports.memory_management.list_user_memories(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        memory_type=memory_type,
        page=page,
    )


@router.delete("/{target_token}", response_model=UserMemoryDeleteResponse)
async def delete_user_memory(
    target_token: str,
    payload: UserMemoryDeleteRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> UserMemoryDeleteResponse:
    try:
        authorize_host_operation(identity, "runtime_write_own")
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc
    if identity.credential_class != "oac_user":
        raise HTTPException(status_code=403, detail="host_operation_forbidden")
    if ports.memory_management is None:
        raise HTTPException(status_code=503, detail="memory_management_unavailable")
    try:
        return await ports.memory_management.delete_user_memory(
            target_token=target_token,
            concurrency_token=payload.concurrency_token,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            idempotency_key=payload.idempotency_key,
        )
    except MemoryManagementNotFound as exc:
        raise HTTPException(status_code=404, detail="user_memory_not_found") from exc
    except MemoryManagementConflict as exc:
        raise HTTPException(status_code=409, detail="user_memory_version_conflict") from exc
