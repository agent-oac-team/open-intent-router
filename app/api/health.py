from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.dependencies import get_registry_service
from app.schemas.runtime import ReadinessResponse, RuntimeCatalogReadinessErrorResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={
        503: {
            "model": RuntimeCatalogReadinessErrorResponse,
            "description": "Runtime Catalog activation failed during application startup.",
        }
    },
)
async def ready(request: Request) -> ReadinessResponse:
    runtime = request.app.state.runtime_catalog_runtime
    runtime_status = runtime.status
    if runtime_status.status != "ready":
        payload = RuntimeCatalogReadinessErrorResponse(
            status="error",
            runtime_status="error",
            runtime_reason=runtime_status.reason_code or "runtime_catalog_not_ready",
        )
        return JSONResponse(
            status_code=503,
            content=payload.model_dump(),
        )
    registry = get_registry_service()
    state = await registry.load()
    return ReadinessResponse(
        status=state.status,
        registry_status=state.status,
        active_source=state.active_source,
        message=state.message,
    )
