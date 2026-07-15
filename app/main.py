from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    admin,
    agents,
    events,
    health,
    knowledge,
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
from app.dependencies import build_memory_formation_runtime, build_memory_maintenance_runtime


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.storage_backend == "database":
        await create_all_tables(settings)
    formation_runtime = build_memory_formation_runtime(settings=settings)
    maintenance_runtime = build_memory_maintenance_runtime(settings=settings)
    await formation_runtime.start()
    await maintenance_runtime.start()
    try:
        yield
    finally:
        await maintenance_runtime.stop()
        await formation_runtime.stop()


def create_app() -> FastAPI:
    settings = get_settings()
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
    app.include_router(health.router)
    app.include_router(router.router)
    app.include_router(agents.router)
    app.include_router(admin.router)
    app.include_router(runs.router)
    app.include_router(events.router)
    app.include_router(plans.router)
    app.include_router(memory.router)
    app.include_router(knowledge.router)
    app.include_router(sessions.router)
    app.include_router(runtime.router)
    return app


app = create_app()
