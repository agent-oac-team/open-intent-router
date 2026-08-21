from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    admin,
    agents,
    events,
    health,
    jwks,
    memory,
    plans,
    router,
    runs,
    runtime,
    sessions,
)
from app.application import ExternalExecutorApplicationPort, RegistrySnapshotSourceMapper
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationRuntimeUnavailable, register_error_handlers
from app.core.memory_runtime import MemoryRuntimePolicy, build_memory_runtime_policy
from app.db.managed import ManagedDatabase, build_managed_database_targets
from app.repositories.database import DatabaseAgentDefinitionRepository
from app.repositories.file_registry import FileRegistrySource
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.runtime.application import (
    ApplicationComposition,
    ApplicationCompositionFactory,
    ApplicationContainer,
    ApplicationRuntime,
)
from app.runtime.catalog import (
    RuntimeAdapterContext,
    RuntimeAdapterDescriptor,
    RuntimeCatalog,
    RuntimeCatalogRuntime,
    build_default_runtime_descriptors,
)
from app.runtime.services import (
    ExternalExecutorFactory,
    build_application_service_composition,
    resolve_memory_data_settings,
)
from app.services.registry_service import AgentRegistryService
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime_factory = app.state.application_runtime_factory
    application_runtime: ApplicationRuntime = runtime_factory()
    app.state.application_runtime = application_runtime
    try:
        async with application_runtime as view:
            app.state.application_runtime_view = view
            app.state.runtime_catalog_runtime = application_runtime.runtime_catalog
            app.state.runtime_readiness_runtime = application_runtime.readiness_runtime
            try:
                app.state.registry_snapshot_runtime = (
                    view.require_container().registry_snapshot_runtime
                )
            except ApplicationRuntimeUnavailable:
                app.state.registry_snapshot_runtime = None
            yield
    finally:
        app.state.application_runtime_view = None
        app.state.application_runtime = None
        app.state.runtime_catalog_runtime = None
        app.state.runtime_readiness_runtime = None
        app.state.registry_snapshot_runtime = None


def create_app(
    *,
    settings: Settings | None = None,
    api_prefix: str = "",
    runtime_descriptors: Sequence[RuntimeAdapterDescriptor] | None = None,
    external_executor: ExternalExecutorApplicationPort | None = None,
    external_executor_factory: ExternalExecutorFactory | None = None,
    registry_snapshot_mapper: RegistrySnapshotSourceMapper | None = None,
    application_composition_factory: ApplicationCompositionFactory | None = None,
    memory_runtime_policy: MemoryRuntimePolicy | None = None,
    memory_database_url: str | None = None,
    memory_collection: str | None = None,
    execution_ticket_secret: str | None = None,
    external_execution_ticket_ttl_seconds: int = 900,
) -> FastAPI:
    """Build one app whose lifespan owns a fresh Runtime on every entry."""

    settings_snapshot = (settings or get_settings()).model_copy(deep=True)
    if application_composition_factory is not None:
        if any(
            (
                external_executor is not None,
                external_executor_factory is not None,
                memory_runtime_policy is not None,
                memory_database_url is not None,
                memory_collection is not None,
                execution_ticket_secret is not None,
                external_execution_ticket_ttl_seconds != 900,
            )
        ):
            raise ValueError("A custom Application Composition owns graph configuration")
        composition = application_composition_factory(settings_snapshot)
    else:
        runtime_policy = memory_runtime_policy or build_memory_runtime_policy(
            settings_snapshot.memory_mode
        )
        memory_settings = resolve_memory_data_settings(
            settings_snapshot,
            runtime_policy,
            database_url=memory_database_url,
            collection=memory_collection,
        )
        composition = build_application_service_composition(
            settings=settings_snapshot,
            external_executor=external_executor,
            external_executor_factory=external_executor_factory,
            memory_runtime_policy=runtime_policy,
            memory_data_settings=memory_settings,
            execution_ticket_secret=execution_ticket_secret,
            external_execution_ticket_ttl_seconds=external_execution_ticket_ttl_seconds,
        )
    descriptors = tuple(runtime_descriptors or build_default_runtime_descriptors())
    normalized_prefix = _normalize_api_prefix(api_prefix)
    app = FastAPI(title="Open Intent Router", version="0.1.0", lifespan=lifespan)
    app.state.application_settings = settings_snapshot
    app.state.application_runtime = None
    app.state.application_runtime_view = None
    app.state.runtime_catalog_runtime = None
    app.state.runtime_readiness_runtime = None
    app.state.registry_snapshot_runtime = None
    # Existing API dependencies continue to be overrideable in tests while each
    # unmodified endpoint receives this app's immutable configuration snapshot.
    app.dependency_overrides[get_settings] = lambda: settings_snapshot
    app.state.application_runtime_factory = _application_runtime_factory(
        settings=settings_snapshot,
        descriptors=descriptors,
        registry_snapshot_mapper=registry_snapshot_mapper,
        application_composition=composition,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in settings_snapshot.cors_allow_origins.split(",")
            if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    app.include_router(jwks.router, prefix=normalized_prefix)
    app.include_router(health.router, prefix=normalized_prefix)
    app.include_router(router.router, prefix=normalized_prefix)
    app.include_router(agents.router, prefix=normalized_prefix)
    app.include_router(admin.router, prefix=normalized_prefix)
    app.include_router(runs.router, prefix=normalized_prefix)
    app.include_router(events.router, prefix=normalized_prefix)
    app.include_router(plans.router, prefix=normalized_prefix)
    app.include_router(memory.router, prefix=normalized_prefix)
    app.include_router(sessions.router, prefix=normalized_prefix)
    app.include_router(runtime.router, prefix=normalized_prefix)
    return app


def _application_runtime_factory(
    *,
    settings: Settings,
    descriptors: Sequence[RuntimeAdapterDescriptor],
    registry_snapshot_mapper: RegistrySnapshotSourceMapper | None,
    application_composition: ApplicationComposition,
):
    def factory() -> ApplicationRuntime:
        catalog_runtime = RuntimeCatalogRuntime(
            descriptors=descriptors,
            context=RuntimeAdapterContext(settings=settings),
            shutdown_timeout_seconds=settings.runtime_catalog_shutdown_timeout_seconds,
            health_check_timeout_seconds=settings.runtime_catalog_health_timeout_seconds,
            required_adapter_keys=settings.runtime_required_adapter_key_set,
        )
        return ApplicationRuntime(
            settings=settings,
            runtime_catalog=catalog_runtime,
            database_factory=lambda: _minimal_databases(
                settings,
                memory_settings=application_composition.memory_database_settings,
                required_targets=application_composition.required_database_targets,
            ),
            container_builder=application_composition.container_builder,
            snapshot_mapper=registry_snapshot_mapper,
        )

    return factory


def _minimal_databases(
    settings: Settings,
    *,
    memory_settings: Settings | None = None,
    required_targets: Collection[str] = ("core",),
) -> dict[str, ManagedDatabase]:
    return build_managed_database_targets(
        settings,
        memory_settings=memory_settings,
        required_targets=required_targets,
    )


def _build_minimal_container(
    *,
    settings: Settings,
    catalog: RuntimeCatalog,
    databases: dict[str, ManagedDatabase],
    external_executor: ExternalExecutorApplicationPort | None,
) -> ApplicationContainer:
    if settings.storage_backend == "database":
        repository = DatabaseAgentDefinitionRepository(databases["core"].session_factory)
    else:
        repository = MemoryAgentDefinitionRepository()
    registry = AgentRegistryService(
        settings=settings,
        repository=repository,
        file_source=FileRegistrySource(settings.registry_file_path),
    )
    snapshot_runtime = RegistrySnapshotRuntime(
        RegistrySnapshotBuilder(catalog, external_executor=external_executor)
    )
    return ApplicationContainer(
        registry=registry,
        runtime_catalog=catalog,
        registry_snapshot_runtime=snapshot_runtime,
        # Compatibility helper for focused Runtime tests.  Production app
        # lifespans use build_application_container() above.
        _background_runtimes=(),
    )


def _normalize_api_prefix(prefix: str) -> str:
    normalized = prefix.rstrip("/")
    if normalized and not normalized.startswith("/"):
        raise ValueError("API prefix must be empty or start with '/'")
    return normalized


app = create_app()
