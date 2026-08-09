from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_native_principal
from app.dependencies import get_repository_bundle
from app.schemas.security import NativePrincipal

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
