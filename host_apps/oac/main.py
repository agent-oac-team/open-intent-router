from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.memory_runtime import build_memory_runtime_policy
from app.dependencies import (
    configure_execution_ticket_runtime,
    configure_memory_runtime,
    get_memory_runtime_policy,
)
from app.main import create_app as create_oir_app
from host_adapters.oac.api import router as oac_adapter_router
from host_adapters.oac.api.capabilities import build_capability_router
from host_adapters.oac.fallback.policy import classify_operation, write_fence_blocked
from host_adapters.oac.registry_snapshot import map_oac_legacy_registry_snapshot
from host_apps.oac.capabilities import OacHostCapabilityProvider
from host_apps.oac.config import build_oac_host_profile, memory_execution_plane_for_shadow
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_oac_external_executor,
)


def create_app() -> FastAPI:
    """Compose the generic OIR runtime with the OAC protocol adapter."""
    profile = build_oac_host_profile()
    execution_plane = memory_execution_plane_for_shadow(profile.host.shadow_mode)
    configure_memory_runtime(
        build_memory_runtime_policy(
            profile.core.memory_mode,
            execution_plane=execution_plane,
            config_source="OAC_HOST_SHADOW_MODE",
        ),
        database_url=profile.host.state_rehearsal_database_url,
        collection=profile.host.state_rehearsal_memory_collection,
    )
    host_ticket_secret = profile.host.execution_ticket_secret
    configure_execution_ticket_runtime(
        secret=(
            host_ticket_secret.get_secret_value()
            if host_ticket_secret is not None
            else profile.core.execution_ticket_secret
        )
    )
    ports = get_oac_adapter_application_ports()
    host_app = create_oir_app(
        api_prefix=profile.host.native_mount_prefix,
        external_executor=get_oac_external_executor(),
        registry_snapshot_mapper=map_oac_legacy_registry_snapshot,
    )

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
        memory_policy=get_memory_runtime_policy(),
    )
    host_app.include_router(build_capability_router(capability_provider.snapshot))
    host_app.state.host_runtime = profile.host.profile
    host_app.state.host_environment = profile.host.environment
    host_app.state.identity_audience = profile.host.identity_audience
    host_app.state.native_api_prefix = profile.host.native_api_prefix
    return host_app


app = create_app()
