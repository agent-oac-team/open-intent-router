"""Short, atomic persistence for one accepted non-canonical Invocation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import InvocationDeadlineExceededError
from app.db.models import AgentResultModel, AgentRunModel
from app.repositories.database import (
    DatabaseResultRepository,
    DatabaseRunRepository,
    _result_from_row,
    _result_values,
    _run_from_row,
    _run_values,
    _validate_run_identity,
)
from app.repositories.interfaces import InvocationCompletionStore
from app.repositories.invocation_commit_guard import guard_invocation_commit
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.schemas.logs import AgentResult, AgentRun

_RESULT_BACKED_RUN_STATUSES = frozenset(
    {"completed", "failed", "invalid_output", "blocked", "clarify"}
)
_COMPLETION_PERSISTENCE_ATTEMPTS = 3


class InvocationCompletionConflict(ValueError):
    """An accepted Run cannot safely be completed with this terminal record."""


def build_invocation_completion_store(
    *,
    run_repository: object,
    result_repository: object,
) -> InvocationCompletionStore:
    """Return the mandatory atomic finisher for a supported repository pair."""

    if isinstance(run_repository, MemoryRunRepository) and isinstance(
        result_repository, MemoryResultRepository
    ):
        return MemoryInvocationCompletionStore(
            run_repository=run_repository,
            result_repository=result_repository,
        )
    if isinstance(run_repository, DatabaseRunRepository) and isinstance(
        result_repository, DatabaseResultRepository
    ):
        return DatabaseInvocationCompletionStore(run_repository.session_factory)
    raise TypeError("Invocation completion requires matching Runtime repositories")


class DatabaseInvocationCompletionStore:
    """Finish one direct Invocation with one short database transaction.

    A post-commit acknowledgement failure is resolved by reading an already
    committed terminal Run/Result pair instead of invoking an Adapter again.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def complete(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
        may_commit: Callable[[], bool] | None = None,
    ) -> tuple[AgentRun, AgentResult]:
        _validate_completion_identity(run, result)
        for attempt in range(_COMPLETION_PERSISTENCE_ATTEMPTS):
            try:
                return await self._complete_once(run=run, result=result, may_commit=may_commit)
            except Exception as exc:
                replay = await self._read_terminal_replay_after_error(run)
                if replay is not None:
                    _validate_replay_identity(run, result, replay)
                    return replay
                if (
                    isinstance(exc, (IntegrityError, OperationalError))
                    and attempt + 1 < _COMPLETION_PERSISTENCE_ATTEMPTS
                ):
                    # This retries only a local database finish write.  The
                    # Adapter already returned and is never invoked again.
                    await asyncio.sleep(0)
                    continue
                raise
        raise AssertionError("Invocation completion attempts were exhausted")  # pragma: no cover

    async def _complete_once(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
        may_commit: Callable[[], bool] | None,
    ) -> tuple[AgentRun, AgentResult]:
        async with self._session_factory() as session, session.begin():
            guard_invocation_commit(session, may_commit)
            _require_terminal_commit_allowed(may_commit)
            row = await session.scalar(
                select(AgentRunModel).where(AgentRunModel.run_id == run.run_id).with_for_update()
            )
            if row is None:
                raise InvocationCompletionConflict("Accepted Run is unavailable")
            existing = _run_from_row(row)
            replay = await _database_terminal_replay(session, existing)
            if replay is not None:
                _validate_replay_identity(run, result, replay)
                return replay
            if existing.status != "running":
                raise InvocationCompletionConflict("Accepted Run is not running")
            _validate_run_identity(existing, run)
            # ``FOR UPDATE`` serializes PostgreSQL.  The conditional update
            # is the durable backstop for SQLite and separate processes: only
            # one writer may claim the running-to-result-backed edge.
            transition = await session.execute(
                update(AgentRunModel)
                .where(
                    AgentRunModel.run_id == run.run_id,
                    AgentRunModel.status == "running",
                )
                .values(**_run_values(run))
            )
            if transition.rowcount != 1:
                raise InvocationCompletionConflict("Accepted Run terminal transition was lost")
            result_row = AgentResultModel(**_result_values(result))
            session.add(result_row)
            await session.flush()
            await session.refresh(result_row)
            # Recheck immediately before the transaction context commits. A
            # caller-owned monotonic deadline is the authority for this
            # invocation, including deterministic test clocks.
            _require_terminal_commit_allowed(may_commit)
            return run, _result_from_row(result_row)

    async def _read_terminal_replay(self, run: AgentRun) -> tuple[AgentRun, AgentResult] | None:
        async with self._session_factory() as session:
            row = await session.get(AgentRunModel, run.run_id)
            if row is None:
                return None
            return await _database_terminal_replay(session, _run_from_row(row))

    async def _read_terminal_replay_after_error(
        self,
        run: AgentRun,
    ) -> tuple[AgentRun, AgentResult] | None:
        try:
            return await self._read_terminal_replay(run)
        except (IntegrityError, OperationalError):
            return None


class MemoryInvocationCompletionStore:
    """Memory equivalent of the atomic direct Invocation finish transaction."""

    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        result_repository: MemoryResultRepository,
    ) -> None:
        self._run_repository = run_repository
        self._result_repository = result_repository
        self._lock = run_repository._invocation_completion_lock

    async def complete(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
        may_commit: Callable[[], bool] | None = None,
    ) -> tuple[AgentRun, AgentResult]:
        _validate_completion_identity(run, result)
        async with self._lock:
            for attempt in range(_COMPLETION_PERSISTENCE_ATTEMPTS):
                _require_terminal_commit_allowed(may_commit)
                existing = await self._run_repository.get_run(run.run_id)
                if existing is None:
                    raise InvocationCompletionConflict("Accepted Run is unavailable")
                replay = _memory_terminal_replay(self._result_repository, existing)
                if replay is not None:
                    _validate_replay_identity(run, result, replay)
                    return replay
                if existing.status != "running":
                    raise InvocationCompletionConflict("Accepted Run is not running")
                _validate_run_identity(existing, run)
                snapshots = (
                    dict(self._run_repository.runs),
                    list(self._result_repository.results),
                    set(self._result_repository.formation_published),
                    set(self._result_repository.turn_captured),
                )
                try:
                    stored_run = await self._run_repository.update_run(run)
                    stored_result = await self._result_repository.add_result(result)
                    _require_terminal_commit_allowed(may_commit)
                except Exception:
                    (
                        self._run_repository.runs,
                        self._result_repository.results,
                        self._result_repository.formation_published,
                        self._result_repository.turn_captured,
                    ) = snapshots
                    if attempt + 1 < _COMPLETION_PERSISTENCE_ATTEMPTS:
                        continue
                    raise
                return stored_run, stored_result
        raise AssertionError("Invocation completion attempts were exhausted")  # pragma: no cover


async def _database_terminal_replay(
    session: AsyncSession,
    run: AgentRun,
) -> tuple[AgentRun, AgentResult] | None:
    if run.status not in _RESULT_BACKED_RUN_STATUSES:
        return None
    rows = (
        (
            await session.execute(
                select(AgentResultModel).where(AgentResultModel.run_id == run.run_id)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        return None
    return run, _result_from_row(rows[0])


def _memory_terminal_replay(
    result_repository: MemoryResultRepository,
    run: AgentRun,
) -> tuple[AgentRun, AgentResult] | None:
    if run.status not in _RESULT_BACKED_RUN_STATUSES:
        return None
    results = [item for item in result_repository.results if item.run_id == run.run_id]
    if len(results) != 1:
        return None
    return run.model_copy(deep=True), results[0].model_copy(deep=True)


def _validate_completion_identity(run: AgentRun, result: AgentResult) -> None:
    if result.run_id != run.run_id:
        raise InvocationCompletionConflict("Result does not belong to Accepted Run")
    fields = ("session_id", "agent_id", "user_id", "tenant_id", "turn_id", "plan_id", "step_id")
    if any(getattr(result, field) != getattr(run, field) for field in fields):
        raise InvocationCompletionConflict("Result identity does not match Accepted Run")


def _require_terminal_commit_allowed(may_commit: Callable[[], bool] | None) -> None:
    """Abort a still-running terminal transaction once its call expired."""

    if may_commit is not None and not may_commit():
        raise InvocationDeadlineExceededError()


def _validate_replay_identity(
    expected_run: AgentRun,
    expected_result: AgentResult,
    replay: tuple[AgentRun, AgentResult],
) -> None:
    stored_run, stored_result = replay
    _validate_completion_identity(stored_run, stored_result)
    if (
        stored_run.status != expected_run.status
        or stored_run.output != expected_run.output
        or stored_run.error != expected_run.error
        or stored_result.status != expected_result.status
        or stored_result.model_dump(mode="json", exclude={"created_at"})
        != expected_result.model_dump(mode="json", exclude={"created_at"})
    ):
        raise InvocationCompletionConflict("Accepted Run terminal replay conflicts")
