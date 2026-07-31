import logging
from collections.abc import AsyncIterator
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
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.db.session import create_all_tables
from app.dependencies import (
    build_memory_formation_runtime,
    build_memory_maintenance_runtime,
    get_memory_data_settings,
    get_memory_runtime_policy,
)
from app.services.mem0_config import memory_infrastructure_metadata

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.storage_backend == "database":
        await create_all_tables(settings)
        memory_settings = get_memory_data_settings()
        if memory_settings.database_url != settings.database_url:
            await create_all_tables(memory_settings)
    policy = get_memory_runtime_policy()
    memory_infrastructure = memory_infrastructure_metadata(settings)
    logger.info(
        "memory_runtime mode=%s policy=%s source=%s execution=%s recall=%s formation=%s "
        "formation_worker=%s index_worker=%s ttl_sweeper=%s context_memory=%s",
        policy.mode,
        policy.version,
        policy.config_source,
        policy.execution_plane,
        policy.effective_recall_enabled,
        policy.effective_formation_mode,
        policy.effective_formation_worker_enabled,
        policy.effective_index_worker_enabled,
        policy.effective_ttl_sweeper_enabled,
        policy.effective_governed_context_memory_enabled,
    )
    logger.info(
        "memory_infrastructure sources=%s collection=%s embedding_model=%s embedding_dims=%s",
        memory_infrastructure["configuration_sources"],
        memory_infrastructure["milvus_collection"],
        memory_infrastructure["embedding_model"],
        memory_infrastructure["embedding_dims"],
    )
    formation_runtime = build_memory_formation_runtime()
    maintenance_runtime = build_memory_maintenance_runtime()
    await formation_runtime.start()
    await maintenance_runtime.start()
    try:
        yield
    finally:
        await maintenance_runtime.stop()
        await formation_runtime.stop()


def create_app(*, api_prefix: str = "") -> FastAPI:
    settings = get_settings()
    normalized_prefix = _normalize_api_prefix(api_prefix)
    app = FastAPI(title="Open Intent Router", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in settings.cors_allow_origins.split(",") if origin.strip()
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


def _normalize_api_prefix(prefix: str) -> str:
    normalized = prefix.rstrip("/")
    if normalized and not normalized.startswith("/"):
        raise ValueError("API prefix must be empty or start with '/'")
    return normalized


app = create_app()
