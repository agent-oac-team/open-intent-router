from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core.memory_runtime import MemoryRuntimePolicy


@dataclass
class MemoryMaintenanceRuntimeStatus:
    state: str = "stopped"
    index_worker_running: bool = False
    ttl_sweeper_running: bool = False
    last_error_code: str | None = None


class MemoryMaintenanceRuntime:
    def __init__(
        self,
        *,
        settings,
        memory_service,
        status=None,
        runtime_policy: MemoryRuntimePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_policy = runtime_policy or settings.memory_runtime_policy
        self.memory_service = memory_service
        self.status = status or MemoryMaintenanceRuntimeStatus()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        if self.runtime_policy.effective_index_worker_enabled:
            self._tasks.append(asyncio.create_task(self._index_loop(), name="memory-index-worker"))
        if self.runtime_policy.effective_ttl_sweeper_enabled:
            self._tasks.append(asyncio.create_task(self._ttl_loop(), name="memory-ttl-sweeper"))
        self.status.index_worker_running = self.runtime_policy.effective_index_worker_enabled
        self.status.ttl_sweeper_running = self.runtime_policy.effective_ttl_sweeper_enabled
        self.status.state = "running" if self._tasks else "disabled"

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.status.index_worker_running = False
        self.status.ttl_sweeper_running = False
        self.status.state = "stopped"

    async def _index_loop(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.memory_service.index_worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status.last_error_code = "memory_index_worker_error"
                processed = None
            if processed is None:
                await self._wait_interval()

    async def _ttl_loop(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.memory_service.ttl_sweeper.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status.last_error_code = "memory_ttl_sweeper_error"
                processed = []
            if not processed:
                await self._wait_interval()

    async def _wait_interval(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self.settings.memory_maintenance_interval_seconds
            )
        except TimeoutError:
            pass


__all__ = ["MemoryMaintenanceRuntime", "MemoryMaintenanceRuntimeStatus"]
