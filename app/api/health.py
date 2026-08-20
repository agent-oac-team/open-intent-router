from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.schemas.runtime import ReadinessResponse, RuntimeCatalogReadinessErrorResponse
from app.services.runtime_readiness import RuntimeReadinessRuntime

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
            "description": "Core, Runtime Catalog, required Adapter, or Primary Registry is unavailable.",
        }
    },
)
async def ready(request: Request) -> ReadinessResponse:
    readiness_runtime = getattr(request.app.state, "runtime_readiness_runtime", None)
    if not isinstance(readiness_runtime, RuntimeReadinessRuntime):
        payload = RuntimeCatalogReadinessErrorResponse(
            status="error",
            runtime_status="error",
            runtime_reason="core_runtime_unavailable",
        )
        return JSONResponse(
            status_code=503,
            content=payload.model_dump(),
        )
    report = await readiness_runtime.refresh()
    if report.status == "error":
        payload = RuntimeCatalogReadinessErrorResponse(
            status="error",
            runtime_status="error",
            runtime_reason=report.reason_code or "runtime_catalog_not_ready",
        )
        return JSONResponse(
            status_code=503,
            content=payload.model_dump(),
        )
    return ReadinessResponse(
        status=report.status,
        registry_status=report.registry_status,
        active_source=report.active_source,
        runtime_status=report.runtime_status,
        reason_code=report.reason_code,
        impacted_definition_count=report.impacted_definition_count,
    )
