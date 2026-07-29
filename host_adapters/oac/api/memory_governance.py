from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas.memory import MemoryGovernanceResponse
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity import authorize_host_operation
from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

router = APIRouter(prefix="/api/v1/admin/memories", tags=["memory-governance"])


@router.get("/governance", response_model=MemoryGovernanceResponse)
async def memory_governance(
    tenant_id: str,
    memory_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> MemoryGovernanceResponse:
    try:
        authorize_host_operation(identity, "read_only")
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc
    if tenant_id != identity.tenant_id:
        raise HTTPException(status_code=403, detail="tenant_scope_forbidden")
    if ports.memory_governance is None:
        raise HTTPException(status_code=503, detail="memory_governance_unavailable")
    return await ports.memory_governance.query(
        tenant_id=identity.tenant_id,
        memory_id=memory_id,
        page=page,
        page_size=page_size,
    )
