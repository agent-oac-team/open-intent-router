"""Aggregate process Runtime, Registry and Snapshot state for readiness endpoints."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from inspect import isawaitable
from typing import Literal

from app.application import (
    RegistryApplicationPort,
    RegistrySnapshotSourceInput,
    RegistrySnapshotSourceMapper,
)
from app.runtime.catalog import RuntimeCatalogRuntime
from app.services.registry_service import AgentRegistryService, RegistryState
from app.services.registry_snapshot import (
    RegistryQuarantineEntry,
    RegistrySnapshotInventoryEntry,
    RegistrySnapshotRuntime,
)

logger = logging.getLogger(__name__)

ReadinessStatus = Literal["ok", "degraded", "error"]
RuntimeReadinessStatus = Literal["ready", "degraded", "error"]


@dataclass(frozen=True, slots=True)
class RuntimeReadinessReport:
    """Safe readiness projection with fixed reason codes and bounded counts."""

    status: ReadinessStatus
    runtime_status: RuntimeReadinessStatus
    reason_code: str | None
    registry_status: Literal["ok", "degraded", "error"]
    active_source: str | None
    impacted_definition_count: int = 0


class RuntimeReadinessRuntime:
    """Own app-lifespan readiness state; request handlers only read or refresh it."""

    def __init__(
        self,
        runtime_catalog: RuntimeCatalogRuntime,
        *,
        snapshot_mapper: RegistrySnapshotSourceMapper | None = None,
    ) -> None:
        self._runtime_catalog = runtime_catalog
        self._snapshot_mapper = snapshot_mapper
        self._primary_registry_status: Literal["ok", "degraded", "error"] = "error"
        self._primary_registry_source: str | None = None
        self._primary_registry_initialized = False
        self._snapshot_runtime: RegistrySnapshotRuntime | None = None
        self._core_failed = False
        self._snapshot_refresh_lock = asyncio.Lock()

    def attach_snapshot_runtime(self, snapshot_runtime: RegistrySnapshotRuntime | None) -> None:
        self._snapshot_runtime = snapshot_runtime
        if snapshot_runtime is not None:
            snapshot_runtime.apply_adapter_health(
                self._runtime_catalog.health.unhealthy_adapter_keys
            )

    def record_primary_registry_state(self, state: RegistryState) -> None:
        self._primary_registry_initialized = True
        self._primary_registry_status = _safe_registry_status(state.status)
        self._primary_registry_source = _safe_registry_source(state.active_source)

    async def initialize_primary_registry(
        self,
        registry: AgentRegistryService,
    ) -> RegistryState | None:
        """Build the primary Registry once during lifespan, retaining only safe status."""

        async with self._snapshot_refresh_lock:
            return await self._load_primary_registry_locked(
                registry,
                reload=False,
                failure_reason="primary_registry_initialization_failed",
            )

    async def reload_primary_registry(
        self,
        registry: AgentRegistryService,
    ) -> RegistryState | None:
        """Reload the canonical source and replace its Snapshot under one fence."""

        async with self._snapshot_refresh_lock:
            return await self._load_primary_registry_locked(
                registry,
                reload=True,
                failure_reason="primary_registry_reload_failed",
            )

    async def refresh_registry_snapshot(self, registry: RegistryApplicationPort) -> bool:
        """Re-read the canonical source after a committed Registry mutation.

        The source read occurs inside the same fence as the Snapshot replacement:
        a delayed older request cannot overwrite a newer committed Registry view.
        """

        async with self._snapshot_refresh_lock:
            try:
                state = await registry.load()
            except Exception:
                logger.warning("primary_registry_refresh_failed")
                self.mark_primary_registry_unavailable()
                return False
            self.record_primary_registry_state(state)
            return await self._refresh_snapshot_from_state_locked(state)

    async def _load_primary_registry_locked(
        self,
        registry: AgentRegistryService,
        *,
        reload: bool,
        failure_reason: str,
    ) -> RegistryState | None:
        try:
            state = await (registry.reload() if reload else registry.load())
        except Exception:
            logger.warning(failure_reason)
            self.mark_primary_registry_unavailable()
            return None
        self.record_primary_registry_state(state)
        if not await self._refresh_snapshot_from_state_locked(state):
            return None
        return state

    async def _refresh_snapshot_from_state_locked(self, state: RegistryState) -> bool:
        """Apply one trusted source mapping while the source-refresh fence is held."""

        snapshot_runtime = self._snapshot_runtime
        mapper = self._snapshot_mapper
        if mapper is None or snapshot_runtime is None:
            return True
        try:
            candidate = mapper(state)
            if isawaitable(candidate):
                candidate = await candidate
            if not isinstance(candidate, RegistrySnapshotSourceInput):
                raise TypeError("Registry Snapshot mapper returned an invalid candidate")
            snapshot_runtime.load(candidate.definitions, source=candidate.source)
        except Exception:
            logger.warning("registry_snapshot_refresh_failed")
            snapshot_runtime.mark_reload_failed()
            return False
        return True

    def mark_primary_registry_unavailable(self) -> None:
        self._primary_registry_initialized = True
        self._primary_registry_status = "error"
        self._primary_registry_source = None

    def mark_core_initialization_failed(self) -> None:
        self._core_failed = True

    async def refresh(self) -> RuntimeReadinessReport:
        """Probe Adapter health only; never reload the Registry from a request."""

        catalog_health = await self._runtime_catalog.refresh_health()
        snapshot_runtime = self._snapshot_runtime
        if snapshot_runtime is not None:
            snapshot_runtime.apply_adapter_health(catalog_health.unhealthy_adapter_keys)
        return self.report()

    def report(self) -> RuntimeReadinessReport:
        """Return cached state without triggering Registry, Adapter or external work."""

        catalog_status = self._runtime_catalog.status
        catalog_health = self._runtime_catalog.health
        if catalog_status.status != "ready":
            return self._error(
                catalog_status.reason_code or "runtime_catalog_not_ready",
            )
        if self._core_failed:
            return self._error("core_initialization_failed")
        if catalog_health.status == "error":
            return self._error(catalog_health.reason_code or "runtime_catalog_unavailable")
        if not self._primary_registry_initialized or self._primary_registry_status == "error":
            return self._error("primary_registry_unavailable")

        snapshot_runtime = self._snapshot_runtime
        snapshot_status = snapshot_runtime.status if snapshot_runtime is not None else None
        impacted_count = (
            snapshot_status.isolated_definition_count + snapshot_status.quarantined_definition_count
            if snapshot_status is not None
            else 0
        )
        if snapshot_status is not None and snapshot_status.status == "error":
            return self._error("registry_snapshot_unavailable", impacted_count=impacted_count)

        if catalog_health.status == "degraded":
            return self._degraded(
                "runtime_adapter_unhealthy",
                impacted_definition_count=(
                    snapshot_status.unhealthy_adapter_definition_count
                    if snapshot_status is not None
                    else 0
                ),
            )
        if self._primary_registry_status == "degraded":
            return self._degraded(
                "primary_registry_degraded",
                impacted_definition_count=impacted_count,
            )
        if (
            snapshot_status is not None
            and snapshot_status.reason_code == "registry_snapshot_reload_failed"
        ):
            return self._degraded(
                "registry_snapshot_reload_failed",
                impacted_definition_count=impacted_count,
            )
        return RuntimeReadinessReport(
            status="ok",
            runtime_status="ready",
            reason_code=None,
            registry_status=self._primary_registry_status,
            active_source=self._primary_registry_source,
        )

    def admin_inventory(self) -> tuple[RegistrySnapshotInventoryEntry, ...]:
        snapshot_runtime = self._snapshot_runtime
        return snapshot_runtime.admin_inventory() if snapshot_runtime is not None else ()

    def admin_quarantine_inventory(self) -> tuple[RegistryQuarantineEntry, ...]:
        snapshot_runtime = self._snapshot_runtime
        return snapshot_runtime.admin_quarantine_inventory() if snapshot_runtime is not None else ()

    def _error(
        self,
        reason_code: str,
        *,
        impacted_count: int = 0,
    ) -> RuntimeReadinessReport:
        return RuntimeReadinessReport(
            status="error",
            runtime_status="error",
            reason_code=reason_code,
            registry_status=self._primary_registry_status,
            active_source=self._primary_registry_source,
            impacted_definition_count=impacted_count,
        )

    def _degraded(
        self,
        reason_code: str,
        *,
        impacted_definition_count: int,
    ) -> RuntimeReadinessReport:
        return RuntimeReadinessReport(
            status="degraded",
            runtime_status="degraded",
            reason_code=reason_code,
            registry_status=self._primary_registry_status,
            active_source=self._primary_registry_source,
            impacted_definition_count=impacted_definition_count,
        )


def _safe_registry_source(source: object) -> str | None:
    return source if isinstance(source, str) and source in {"file", "database"} else None


def _safe_registry_status(status: object) -> Literal["ok", "degraded", "error"]:
    if status == "ok":
        return "ok"
    if status == "degraded":
        return "degraded"
    return "error"
