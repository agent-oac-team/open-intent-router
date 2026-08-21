"""Application-scoped SQLAlchemy resources with explicit disposal ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _create_async_engine,
)

from app.core.config import Settings
from app.db.models import Base
from app.db.session import _ensure_compatible_columns, _ensure_sqlite_parent


@dataclass(frozen=True, slots=True)
class _EngineSpec:
    """The complete, private set of options that affects Engine identity."""

    database_url: str = field(repr=False)
    pool_pre_ping: bool
    future: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> _EngineSpec:
        return cls(
            database_url=str(settings.database_url),
            pool_pre_ping=not settings.database_url.startswith("sqlite+aiosqlite:"),
        )


class ManagedDatabase:
    """Own one Engine for one lexical resource scope.

    Callers receive an ``async_sessionmaker`` for repository construction, schema
    initialization, and a small readiness probe.  The Engine itself remains an
    implementation detail so its disposal responsibility cannot escape the
    owning application or fixture scope.
    """

    def __init__(self, spec: _EngineSpec) -> None:
        self._spec = spec
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._closed = False

    @classmethod
    def from_settings(cls, settings: Settings) -> ManagedDatabase:
        return cls(_EngineSpec.from_settings(settings))

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        factory = self._session_factory
        if factory is None or self._closed:
            raise RuntimeError("Managed Database is unavailable")
        return factory

    async def __aenter__(self) -> ManagedDatabase:
        if self._closed or self._engine is not None:
            raise RuntimeError("Managed Database instances are single use")
        try:
            _ensure_sqlite_parent(self._spec.database_url)
            options: dict[str, bool] = {"future": self._spec.future}
            if self._spec.pool_pre_ping:
                options["pool_pre_ping"] = True
            engine = _create_async_engine(self._spec.database_url, **options)
            self._engine = engine
            self._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.aclose()

    async def initialize_schema(self) -> None:
        engine = self._require_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(_ensure_compatible_columns)

    async def probe(self, *, timeout_seconds: float) -> bool:
        """Run one bounded, read-only readiness probe without exposing errors."""

        if timeout_seconds <= 0:
            raise ValueError("Database probe timeout must be positive")
        engine = self._require_engine()
        try:
            async with asyncio.timeout(timeout_seconds):
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except TimeoutError:
            return False
        except Exception:
            return False
        return True

    async def aclose(self) -> None:
        engine = self._engine
        self._session_factory = None
        self._engine = None
        self._closed = True
        if engine is not None:
            await engine.dispose()

    def _require_engine(self) -> AsyncEngine:
        engine = self._engine
        if engine is None or self._closed:
            raise RuntimeError("Managed Database is unavailable")
        return engine


def build_managed_database_targets(
    settings: Settings,
    *,
    memory_settings: Settings | None = None,
    required_targets: Collection[str] = ("core", "memory"),
) -> dict[str, ManagedDatabase]:
    """Create the complete Core/Memory target map for one app lifespan.

    Target names remain an internal composition detail.  Equal private Engine
    specs intentionally resolve to the same ManagedDatabase object, so schema
    setup, probes, and disposal occur exactly once for that physical target.
    """

    selected_targets = frozenset(required_targets)
    unknown_targets = selected_targets.difference({"core", "memory"})
    if unknown_targets:
        names = ", ".join(sorted(unknown_targets))
        raise ValueError(f"Unknown Managed Database targets: {names}")
    if settings.storage_backend != "database":
        return {}

    selected_memory_settings = memory_settings or _memory_target_settings(settings)
    instances_by_spec: dict[_EngineSpec, ManagedDatabase] = {}
    targets: dict[str, ManagedDatabase] = {}
    target_settings_by_name = {
        "core": settings,
        "memory": selected_memory_settings,
    }
    for name in ("core", "memory"):
        if name not in selected_targets:
            continue
        target_settings = target_settings_by_name[name]
        spec = _EngineSpec.from_settings(target_settings)
        targets[name] = instances_by_spec.setdefault(spec, ManagedDatabase(spec))
    return targets


def _memory_target_settings(settings: Settings) -> Settings:
    memory_url = settings.effective_memory_database_url
    if memory_url is None or memory_url == settings.database_url:
        return settings
    return settings.model_copy(update={"database_url": memory_url})
