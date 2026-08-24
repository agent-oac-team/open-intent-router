from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_native_principal
from app.dependencies import get_invocation_service, get_repository_bundle
from app.schemas.invocation import InvocationCancelRequest, InvocationCancelResponse
from app.schemas.security import NativePrincipal
from app.services.invocation_service import InvocationService

router = APIRouter(prefix="/api/v1", tags=["runs"])


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    principal: NativePrincipal = Depends(require_native_principal),
    repositories: dict = Depends(get_repository_bundle),
):
    run = await repositories["runs"].get_owned_run(
        run_id,
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.post("/runs/{run_id}/cancel", response_model=InvocationCancelResponse)
async def cancel_run(
    run_id: str,
    _payload: InvocationCancelRequest | None = None,
    principal: NativePrincipal = Depends(require_native_principal),
    invocation_service: InvocationService = Depends(get_invocation_service),
) -> InvocationCancelResponse:
    """Request capability-gated stop control for an owned Invocation Run."""

    response = await invocation_service.cancel_run(
        run_id,
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
    )
    if response is None:
        # Match the read path: an unauthorized caller cannot distinguish a
        # foreign Run from an absent one and never reaches Runtime control.
        raise HTTPException(status_code=404, detail="Run not found")
    return response
