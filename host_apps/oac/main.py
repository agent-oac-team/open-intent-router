from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.errors import ApplicationRuntimeUnavailable
from app.core.memory_runtime import build_memory_runtime_policy
from app.main import create_app as create_oir_app
from host_adapters.oac.api import router as oac_adapter_router
from host_adapters.oac.api.capabilities import build_capability_router
from host_adapters.oac.external_executor import OacExternalExecutor
from host_adapters.oac.fallback.policy import classify_operation, write_fence_blocked
from host_adapters.oac.registry_snapshot import map_oac_legacy_registry_snapshot
from host_apps.oac.config import (
    OacHostProfile,
    build_oac_host_profile,
    get_oac_host_settings,
    memory_execution_plane_for_shadow,
)
from host_apps.oac.dependencies import build_oac_application_container


def create_app(*, profile: OacHostProfile | None = None) -> FastAPI:
    """Compose one OAC Host lifespan around one complete Core Runtime."""

    supplied_profile = profile or build_oac_host_profile()
    profile = build_oac_host_profile(
        core=supplied_profile.core.model_copy(deep=True),
        host=supplied_profile.host.model_copy(deep=True),
    )
    execution_plane = memory_execution_plane_for_shadow(profile.host.shadow_mode)
    memory_policy = build_memory_runtime_policy(
        profile.core.memory_mode,
        execution_plane=execution_plane,
        config_source="OAC_HOST_SHADOW_MODE",
    )
    host_ticket_secret = profile.host.execution_ticket_secret
    execution_ticket_secret = (
        host_ticket_secret.get_secret_value()
        if host_ticket_secret is not None
        else profile.core.execution_ticket_secret
    )

    def external_executor_factory(acceptance_store):
        external_executor = OacExternalExecutor(
            supported_executor_refs=profile.host.supported_external_executor_refs,
            acceptance_store=acceptance_store,
            acceptance_fingerprint_secret=execution_ticket_secret,
        )
        return external_executor

    host_app = create_oir_app(
        settings=profile.core,
        api_prefix=profile.host.native_mount_prefix,
        registry_snapshot_mapper=map_oac_legacy_registry_snapshot,
        external_executor_factory=external_executor_factory,
        memory_runtime_policy=memory_policy,
        memory_database_url=profile.host.state_rehearsal_database_url,
        memory_collection=profile.host.state_rehearsal_memory_collection,
        execution_ticket_secret=execution_ticket_secret,
        external_execution_ticket_ttl_seconds=profile.host.execution_ticket_ttl_seconds,
    )
    host_app.dependency_overrides[get_oac_host_settings] = lambda: profile.host
    _install_host_lifespan(host_app, profile)

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
    host_app.include_router(build_capability_router(_capability_snapshot(host_app)))
    host_app.state.host_runtime = profile.host.profile
    host_app.state.host_environment = profile.host.environment
    host_app.state.identity_audience = profile.host.identity_audience
    host_app.state.native_api_prefix = profile.host.native_api_prefix
    host_app.state.oac_application_container = None
    return host_app


def _install_host_lifespan(host_app: FastAPI, profile: OacHostProfile) -> None:
    core_lifespan = host_app.router.lifespan_context

    @asynccontextmanager
    async def host_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with core_lifespan(app):
            view = app.state.application_runtime_view
            runtime_catalog = app.state.runtime_catalog_runtime
            readiness = app.state.runtime_readiness_runtime
            try:
                core_container = view.require_container()
            except ApplicationRuntimeUnavailable:
                # Catalog degradation deliberately publishes only the Core View;
                # no partial OAC ports may become reachable in that state.
                app.state.oac_application_container = None
                yield
                return
            if runtime_catalog is None or readiness is None:
                raise RuntimeError("OAC Host Runtime composition is unavailable")
            app.state.oac_application_container = build_oac_application_container(
                profile=profile,
                core_container=core_container,
                runtime_catalog_runtime=runtime_catalog,
                registry_snapshot_refresh=readiness,
            )
            try:
                yield
            finally:
                # Remove Host ports before Core begins reverse shutdown.
                app.state.oac_application_container = None

    host_app.router.lifespan_context = host_lifespan


def _capability_snapshot(host_app: FastAPI):
    async def snapshot():
        container = getattr(host_app.state, "oac_application_container", None)
        if container is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="application_runtime_unavailable",
            )
        return await container.capability_provider.snapshot()

    return snapshot


app = create_app()
