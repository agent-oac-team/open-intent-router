import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.repositories.delegated_runs import DelegatedRunStartConflict
from app.schemas.delegated_runs import (
    DELEGATED_RUN_MAINTENANCE_EVENT_PREFIX,
    DelegatedRunOverdueQuery,
    DelegatedRunReference,
    DelegatedRunTimeoutCommand,
)
from app.services.delegated_run_service import DelegatedRunService

logger = logging.getLogger(__name__)


class DelegatedRunTimeoutRuntime:
    def __init__(
        self,
        service: DelegatedRunService,
        *,
        interval_seconds: float,
        batch_size: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.clock = clock or (lambda: datetime.now(UTC))
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="delegated-run-timeout-runtime",
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stopped.set()
        await task
        self._task = None

    async def run_once(self) -> int:
        now = _as_utc(self.clock())
        overdue = await self.service.list_overdue(
            DelegatedRunOverdueQuery(now=now, limit=self.batch_size)
        )
        transitioned = 0
        for run in overdue.runs:
            try:
                await self.service.timeout(
                    DelegatedRunTimeoutCommand(
                        event_id=timeout_event_id(run),
                        run_id=run.run_id,
                        turn_id=run.turn_id,
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                        agent_id=run.agent_id,
                        plan_id=run.plan_id,
                        step_id=run.step_id,
                        expected_state_version=run.state_version,
                        occurred_at=now,
                        deadline_at=run.deadline_at,
                    )
                )
            except DelegatedRunStartConflict:
                continue
            transitioned += 1
        return transitioned

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("delegated_run_timeout_sweep_failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                continue


def timeout_event_id(run: DelegatedRunReference) -> str:
    deadline = _as_utc(run.deadline_at).isoformat(timespec="microseconds")
    digest = hashlib.sha256(f"{run.run_id}\x1f{deadline}".encode()).hexdigest()[:32]
    return f"{DELEGATED_RUN_MAINTENANCE_EVENT_PREFIX}{digest}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
