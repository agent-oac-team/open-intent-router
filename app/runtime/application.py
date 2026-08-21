"""Single-use application Runtime and the narrow View visible to requests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from inspect import isawaitable
from typing import TYPE_CHECKING, TypeAlias

from app.application import RegistrySnapshotSourceMapper
from app.core.config import Settings
from app.core.errors import (
    ApplicationRuntimeUnavailable,
    ApplicationShutdownError,
    ApplicationStartupError,
)
from app.db.managed import ManagedDatabase
from app.runtime.catalog import RuntimeCatalog, RuntimeCatalogRuntime
from app.services.registry_service import AgentRegistryService
from app.services.registry_snapshot import RegistrySnapshotRuntime
from app.services.runtime_readiness import RuntimeReadinessReport, RuntimeReadinessRuntime

if TYPE_CHECKING:
    from app.runtime.services import ApplicationServices

logger = logging.getLogger(__name__)


class _RuntimeState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    DEGRADED = "degraded"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """A complete set of assembled app capabilities, never raw resources."""

    registry: AgentRegistryService
    runtime_catalog: RuntimeCatalog
    registry_snapshot_runtime: RegistrySnapshotRuntime | None
    services: ApplicationServices | None = field(default=None, repr=False)
    _background_runtimes: Sequence[object] = field(default_factory=tuple, repr=False)


DatabaseFactory: TypeAlias = Callable[[], Mapping[str, ManagedDatabase]]
ContainerBuilder: TypeAlias = Callable[
    [RuntimeCatalog, Mapping[str, ManagedDatabase]],
    ApplicationContainer | Awaitable[ApplicationContainer],
]


@dataclass(frozen=True, slots=True)
class ApplicationComposition:
    """One selected service graph and the database targets it actually consumes."""

    container_builder: ContainerBuilder
    required_database_targets: frozenset[str]
    memory_database_settings: Settings | None = field(default=None, repr=False)


ApplicationCompositionFactory: TypeAlias = Callable[[Settings], ApplicationComposition]


class ApplicationRuntimeView:
    """The only Runtime interface exposed to HTTP and Host composition."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime

    def require_container(self) -> ApplicationContainer:
        container = self._runtime._container
        if self._runtime._state is not _RuntimeState.READY or container is None:
            raise ApplicationRuntimeUnavailable("Application Runtime is unavailable")
        return container

    async def readiness(self) -> RuntimeReadinessReport:
        return await self._runtime._readiness()


class ApplicationRuntime:
    """One lifespan-owned Runtime with deterministic reverse cleanup.

    This class intentionally has no public start/stop API.  A FastAPI lifespan
    enters it once, publishes its View only for that lexical scope, then closes
    it.  Catalog activation keeps its existing safe degradation behavior; all
    later required startup failures abort the lifespan after rollback.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_catalog: RuntimeCatalogRuntime,
        database_factory: DatabaseFactory,
        container_builder: ContainerBuilder,
        snapshot_mapper: RegistrySnapshotSourceMapper | None = None,
    ) -> None:
        self._runtime_catalog = runtime_catalog
        self._database_factory = database_factory
        self._container_builder = container_builder
        self._database_probe_timeout_seconds = settings.database_probe_timeout_seconds
        self._cleanup_timeout_seconds = settings.application_cleanup_timeout_seconds
        self._databases: Mapping[str, ManagedDatabase] = {}
        self._entered_databases: list[ManagedDatabase] = []
        self._started_background_runtimes: list[object] = []
        self._container: ApplicationContainer | None = None
        self._readiness_runtime = RuntimeReadinessRuntime(
            runtime_catalog,
            snapshot_mapper=snapshot_mapper,
        )
        self._state = _RuntimeState.NEW
        self._view = ApplicationRuntimeView(self)

    @property
    def view(self) -> ApplicationRuntimeView:
        return self._view

    @property
    def readiness_runtime(self) -> RuntimeReadinessRuntime:
        """Runtime-owned compatibility port for Registry snapshot refreshes."""

        return self._readiness_runtime

    @property
    def runtime_catalog(self) -> RuntimeCatalogRuntime:
        """Composition-only access while the Runtime owns the Catalog lifecycle."""

        return self._runtime_catalog

    async def __aenter__(self) -> ApplicationRuntimeView:
        if self._state is not _RuntimeState.NEW:
            raise RuntimeError("Application Runtime instances are single use")
        self._state = _RuntimeState.STARTING
        try:
            await self._runtime_catalog.start()
            catalog = self._runtime_catalog.catalog
            if catalog is None or self._runtime_catalog.health.status == "error":
                self._state = _RuntimeState.DEGRADED
                return self._view

            self._databases = dict(self._database_factory())
            self._entered_databases = _unique_databases(self._databases)
            for database in self._entered_databases:
                await database.__aenter__()
            for database in self._entered_databases:
                await database.initialize_schema()

            candidate = self._container_builder(catalog, self._databases)
            container = await candidate if isawaitable(candidate) else candidate
            self._readiness_runtime.attach_snapshot_runtime(container.registry_snapshot_runtime)
            if (
                await self._readiness_runtime.initialize_primary_registry(container.registry)
                is None
            ):
                raise RuntimeError("Primary Registry initialization failed")
            for background_runtime in container._background_runtimes:
                self._started_background_runtimes.append(background_runtime)
                await background_runtime.start()
            self._container = container
            self._state = _RuntimeState.READY
            return self._view
        except asyncio.CancelledError:
            await self._cleanup()
            raise
        except Exception:
            await self._cleanup()
        # Raise after leaving the exception handler so the safe public error
        # retains neither a cause nor an inspectable exception context.
        raise ApplicationStartupError()

    async def __aexit__(self, exc_type, _exc, _traceback) -> None:
        failures = await self._cleanup()
        if exc_type is None and failures:
            raise ApplicationShutdownError(failures)

    async def _readiness(self) -> RuntimeReadinessReport:
        if self._state in {_RuntimeState.NEW, _RuntimeState.CLOSED}:
            return _runtime_unavailable_report()
        report = await self._readiness_runtime.refresh()
        if report.status == "error":
            return report
        if self._state is not _RuntimeState.READY or self._container is None:
            return _runtime_unavailable_report()
        if not self._entered_databases:
            return report
        probe_results = await asyncio.gather(
            *(
                database.probe(timeout_seconds=self._database_probe_timeout_seconds)
                for database in self._entered_databases
            )
        )
        if all(probe_results):
            return report
        return RuntimeReadinessReport(
            status="error",
            runtime_status="error",
            reason_code="database_unavailable",
            registry_status=report.registry_status,
            active_source=report.active_source,
        )

    async def _cleanup(self) -> int:
        if self._state is _RuntimeState.CLOSED:
            return 0
        self._state = _RuntimeState.CLOSING
        self._container = None
        self._readiness_runtime.attach_snapshot_runtime(None)
        failures = 0
        cancellation: asyncio.CancelledError | None = None
        for name, operation in [
            *(
                ("background_runtime", background_runtime.stop)
                for background_runtime in reversed(self._started_background_runtimes)
            ),
            *(("database", database.aclose) for database in reversed(self._entered_databases)),
            ("runtime_catalog", self._runtime_catalog.stop),
        ]:
            cleanup_task = asyncio.create_task(operation())
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup_task),
                    timeout=self._cleanup_timeout_seconds,
                )
            except asyncio.CancelledError as exc:
                # Do not propagate cancellation into the resource's own
                # cleanup task.  Complete it (or time it out) before moving to
                # later cleanup steps, then restore the original cancellation.
                cancellation = cancellation or exc
                try:
                    await asyncio.wait_for(
                        asyncio.shield(cleanup_task),
                        timeout=self._cleanup_timeout_seconds,
                    )
                except TimeoutError:
                    failures += 1
                    cleanup_task.cancel()
                    await asyncio.gather(cleanup_task, return_exceptions=True)
                    logger.warning("application_cleanup_timeout component=%s", name)
                except Exception as cleanup_error:
                    failures += 1
                    logger.warning(
                        "application_cleanup_failed component=%s category=%s",
                        name,
                        type(cleanup_error).__name__,
                    )
            except TimeoutError:
                failures += 1
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
                logger.warning("application_cleanup_timeout component=%s", name)
            except Exception as exc:
                failures += 1
                logger.warning(
                    "application_cleanup_failed component=%s category=%s",
                    name,
                    type(exc).__name__,
                )
        self._entered_databases = []
        self._started_background_runtimes = []
        self._databases = {}
        self._state = _RuntimeState.CLOSED
        if cancellation is not None:
            raise cancellation
        return failures


def _unique_databases(databases: Mapping[str, ManagedDatabase]) -> list[ManagedDatabase]:
    unique: list[ManagedDatabase] = []
    seen: set[int] = set()
    for database in databases.values():
        if id(database) in seen:
            continue
        seen.add(id(database))
        unique.append(database)
    return unique


def _runtime_unavailable_report() -> RuntimeReadinessReport:
    return RuntimeReadinessReport(
        status="error",
        runtime_status="error",
        reason_code="application_runtime_unavailable",
        registry_status="error",
        active_source=None,
    )
