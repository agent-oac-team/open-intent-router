"""Explicit database scopes shared by database-backed tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _create_async_engine,
)

from app.core.config import Settings
from app.db.managed import ManagedDatabase


@dataclass(slots=True)
class _DatabaseOwnershipRegistry:
    """Fixture-local ownership audit independent of thread names or test timing."""

    _open_scopes: dict[int, ManagedDatabase] = field(default_factory=dict)

    def register(self, database: ManagedDatabase) -> None:
        self._open_scopes[id(database)] = database

    def release(self, database: ManagedDatabase) -> None:
        self._open_scopes.pop(id(database), None)

    def assert_clean(self) -> None:
        if self._open_scopes:
            raise AssertionError(
                "Database test scopes reached suite teardown without disposal: "
                f"{len(self._open_scopes)}"
            )


class ManagedTestDatabases:
    """Fixture-owned databases whose factories cannot outlive the test scope."""

    def __init__(self) -> None:
        self._databases: list[ManagedDatabase] = []
        self._ownership = _DatabaseOwnershipRegistry()

    async def initialize_schema(self, settings: Settings) -> None:
        """Initialize schema in a short, explicitly closed scope."""

        async with ManagedDatabase.from_settings(settings) as database:
            await database.initialize_schema()

    async def session_factory(
        self,
        settings: Settings,
    ) -> async_sessionmaker[AsyncSession]:
        """Open one fixture-owned database and return its live Session factory."""

        database = ManagedDatabase.from_settings(settings)
        self._ownership.register(database)
        try:
            await database.__aenter__()
            factory = database.session_factory
        except BaseException:
            self._ownership.release(database)
            await database.aclose()
            raise
        self._databases.append(database)
        return factory

    async def aclose(self) -> None:
        """Attempt every outstanding dispose even after failure or cancellation."""

        cancellation: asyncio.CancelledError | None = None
        failures: list[BaseException] = []
        for database in reversed(self._databases):
            close_task = asyncio.create_task(database.aclose())
            try:
                while not close_task.done():
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                    except BaseException:
                        break
                try:
                    close_task.result()
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                except BaseException as exc:
                    failures.append(exc)
            finally:
                self._ownership.release(database)
        self._databases.clear()
        self._ownership.assert_clean()
        if cancellation is not None:
            raise cancellation
        if failures:
            raise BaseExceptionGroup("Database test fixture cleanup failed", failures)


@asynccontextmanager
async def raw_engine_scope(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Allow narrowly-scoped raw SQLAlchemy operations with visible disposal."""

    options: dict[str, bool] = {"future": True}
    if not database_url.startswith("sqlite+aiosqlite:"):
        options["pool_pre_ping"] = True
    engine = _create_async_engine(database_url, **options)
    try:
        yield engine
    finally:
        await engine.dispose()
