from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.db.models import Base


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    path_text = database_url.removeprefix(prefix)
    if path_text in {":memory:", ""}:
        return
    Path(path_text).parent.mkdir(parents=True, exist_ok=True)


def create_engine(settings: Settings) -> AsyncEngine:
    _ensure_sqlite_parent(settings.database_url)
    return create_async_engine(settings.database_url, future=True)


def create_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(create_engine(settings), expire_on_commit=False)


async def create_all_tables(settings: Settings) -> None:
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_compatible_columns)
    await engine.dispose()


def _ensure_compatible_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    if "agent_definitions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("agent_definitions")}
    if "context_text" not in columns:
        sync_conn.execute(
            text("ALTER TABLE agent_definitions ADD COLUMN context_text TEXT DEFAULT '{}'")
        )


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
