from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.main import create_app as create_oir_app
from host_adapters.oac.api import router as oac_adapter_router
from host_adapters.oac.api.capabilities import build_capability_router
from host_adapters.oac.fallback.policy import classify_operation, write_fence_blocked
from host_apps.oac.capabilities import OacHostCapabilityProvider
from host_apps.oac.config import build_oac_host_profile
from host_apps.oac.dependencies import (
    get_adapter_governance_metrics,
    get_irs_fallback_gateway,
    get_oac_adapter_application_ports,
)


def create_app() -> FastAPI:
    """Compose the generic OIR runtime with the OAC protocol adapter."""
    profile = build_oac_host_profile()
    ports = get_oac_adapter_application_ports()
    host_app = create_oir_app(api_prefix=profile.host.native_mount_prefix)

    @host_app.middleware("http")
    async def capture_host_wire_body(request: Request, call_next):
        request.scope["oac_host_wire_body"] = await request.body()
        return await call_next(request)

    @host_app.middleware("http")
    async def enforce_write_fence(request: Request, call_next):
        try:
            operation = classify_operation(request.method, request.url.path)
        except KeyError:
            return await call_next(request)
        if write_fence_blocked(profile.host, operation):
            metrics = get_adapter_governance_metrics()
            metrics.counters[f"{operation.name}:write_fence_blocked"] += 1
            metrics.counters[f"class:{operation.operation_class.value}:write_fence_blocked"] += 1
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "write_fence_blocked",
                    "operation": operation.name,
                    "operation_class": operation.operation_class.value,
                },
            )
        return await call_next(request)

    host_app.include_router(oac_adapter_router)
    capability_provider = OacHostCapabilityProvider(
        core=profile.core,
        host=profile.host,
        ports=ports,
        fallback_gateway=get_irs_fallback_gateway(),
    )
    host_app.include_router(build_capability_router(capability_provider.snapshot))
    host_app.state.host_runtime = profile.host.profile
    host_app.state.host_environment = profile.host.environment
    host_app.state.identity_audience = profile.host.identity_audience
    host_app.state.native_api_prefix = profile.host.native_api_prefix
    return host_app


app = create_app()
