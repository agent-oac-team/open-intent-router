import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentResultModel, AgentRunModel, CanonicalTurnModel
from app.repositories.database import _result_from_row, _run_from_row, _run_values
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turn_transactions import (
    DatabaseTurnTransactionCoordinator,
    TurnCompletionBundle,
)
from app.repositories.turns import MemoryTurnRepository, _turn_from_row
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.turns import (
    CanonicalTurn,
    FormationEligibilitySnapshot,
    TurnOutboxEvent,
    TurnSemanticResponse,
    TurnStatus,
)


class CanonicalInvocationConflict(ValueError):
    pass


class DatabaseCanonicalInvocationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.completion = DatabaseTurnTransactionCoordinator(session_factory)

    async def start_run(self, run: AgentRun) -> tuple[AgentRun, CanonicalTurn, AgentResult | None]:
        _validate_run_owner(run)
        async with self.session_factory() as session, session.begin():
            turn_row = await session.scalar(
                select(CanonicalTurnModel)
                .where(
                    CanonicalTurnModel.request_id == run.request_id,
                    CanonicalTurnModel.tenant_id == run.tenant_id,
                    CanonicalTurnModel.user_id == run.user_id,
                )
                .with_for_update()
            )
            turn = _validate_start_turn(turn_row, run)
            replay = await _database_replay(session, turn)
            if replay is not None:
                return replay
            attached_run, attached_turn = _attach_run(run, turn)
            session.add(AgentRunModel(**_run_values(attached_run)))
            _apply_active_turn(turn_row, attached_turn)
            await session.flush()
            return attached_run, attached_turn, None

    async def complete_run(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
        response_text: str,
        eligibility: FormationEligibilitySnapshot,
    ) -> tuple[AgentRun, AgentResult, CanonicalTurn]:
        async with self.session_factory() as session:
            turn_row = await session.get(CanonicalTurnModel, run.turn_id)
            if turn_row is None:
                raise CanonicalInvocationConflict("Canonical Turn not found")
            turn = _turn_from_row(turn_row)
        bundle = _completion_bundle(
            run=run,
            result=result,
            turn=turn,
            response_text=response_text,
            eligibility=eligibility,
        )
        try:
            await self.completion.complete(bundle)
        except IntegrityError:
            replay = await self._terminal_replay(bundle.turn)
            if replay is not None:
                return replay
            raise
        return bundle.run, bundle.result, bundle.turn

    async def _terminal_replay(
        self, turn: CanonicalTurn
    ) -> tuple[AgentRun, AgentResult, CanonicalTurn] | None:
        async with self.session_factory() as session:
            row = await session.get(CanonicalTurnModel, turn.turn_id)
            if row is None:
                return None
            stored_turn = _turn_from_row(row)
            if not stored_turn.status.is_terminal or not stored_turn.references.result_ids:
                return None
            run_row = await session.get(AgentRunModel, stored_turn.references.run_ids[-1])
            result_row = await session.get(AgentResultModel, stored_turn.references.result_ids[-1])
            if run_row is None or result_row is None:
                return None
            return _run_from_row(run_row), _result_from_row(result_row), stored_turn


class MemoryCanonicalInvocationStore:
    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        result_repository: MemoryResultRepository,
        turn_repository: MemoryTurnRepository,
        outbox_repository: MemoryTurnOutboxRepository,
    ) -> None:
        self.run_repository = run_repository
        self.result_repository = result_repository
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        self._lock = asyncio.Lock()

    async def start_run(self, run: AgentRun) -> tuple[AgentRun, CanonicalTurn, AgentResult | None]:
        _validate_run_owner(run)
        async with self._lock:
            turn = await self.turn_repository.get_by_request(
                tenant_id=run.tenant_id or "",
                user_id=run.user_id or "",
                request_id=run.request_id or "",
            )
            turn = _validate_start_turn(turn, run)
            replay = await self._start_replay(turn)
            if replay is not None:
                return replay
            attached_run, attached_turn = _attach_run(run, turn)
            await self.run_repository.add_run(attached_run)
            stored_turn = await self.turn_repository.update_if_version(
                attached_turn, expected_version=turn.state_version
            )
            if stored_turn is None:
                self.run_repository.runs.pop(attached_run.run_id, None)
                raise CanonicalInvocationConflict("Canonical Turn changed concurrently")
            return attached_run, stored_turn, None

    async def complete_run(
        self,
        *,
        run: AgentRun,
        result: AgentResult,
        response_text: str,
        eligibility: FormationEligibilitySnapshot,
    ) -> tuple[AgentRun, AgentResult, CanonicalTurn]:
        async with self._lock:
            turn = await self.turn_repository.get(
                run.turn_id or "",
                tenant_id=run.tenant_id or "",
                user_id=run.user_id or "",
            )
            if turn is None:
                raise CanonicalInvocationConflict("Canonical Turn not found")
            if turn.status.is_terminal:
                replay = await self._terminal_replay(turn)
                if replay is not None:
                    return replay
                raise CanonicalInvocationConflict("Canonical Turn is already terminal")
            bundle = _completion_bundle(
                run=run,
                result=result,
                turn=turn,
                response_text=response_text,
                eligibility=eligibility,
            )
            snapshots = (
                dict(self.run_repository.runs),
                list(self.result_repository.results),
                set(self.result_repository.formation_published),
                set(self.result_repository.turn_captured),
                dict(self.turn_repository.turns),
                dict(self.outbox_repository.events),
                dict(self.outbox_repository.idempotency_keys),
            )
            try:
                await self.run_repository.update_run(bundle.run)
                await self.result_repository.add_result(bundle.result)
                stored_turn = await self.turn_repository.update_if_version(
                    bundle.turn, expected_version=turn.state_version
                )
                if stored_turn is None:
                    raise CanonicalInvocationConflict("Canonical Turn changed concurrently")
                await self.outbox_repository.add_idempotent(bundle.outbox)
            except Exception:
                (
                    runs,
                    results,
                    formation_published,
                    turn_captured,
                    turns,
                    outboxes,
                    outbox_keys,
                ) = snapshots
                self.run_repository.runs = runs
                self.result_repository.results = results
                self.result_repository.formation_published = formation_published
                self.result_repository.turn_captured = turn_captured
                self.turn_repository.turns = turns
                self.outbox_repository.events = outboxes
                self.outbox_repository.idempotency_keys = outbox_keys
                raise
            return bundle.run, bundle.result, stored_turn

    async def _start_replay(
        self, turn: CanonicalTurn
    ) -> tuple[AgentRun, CanonicalTurn, AgentResult | None] | None:
        if not turn.references.run_ids:
            return None
        run = await self.run_repository.get_run(turn.references.run_ids[-1])
        if run is None:
            raise CanonicalInvocationConflict("Canonical Turn references a missing Run")
        result = None
        if turn.references.result_ids:
            result_id = turn.references.result_ids[-1]
            result = next(
                (item for item in self.result_repository.results if item.result_id == result_id),
                None,
            )
            if result is None:
                raise CanonicalInvocationConflict("Canonical Turn references a missing Result")
        return run, turn, result.model_copy(deep=True) if result else None

    async def _terminal_replay(
        self, turn: CanonicalTurn
    ) -> tuple[AgentRun, AgentResult, CanonicalTurn] | None:
        replay = await self._start_replay(turn)
        if replay is None:
            return None
        run, stored_turn, result = replay
        if result is None:
            raise CanonicalInvocationConflict("Terminal Turn is missing a Result")
        return run, result, stored_turn


async def _database_replay(
    session: AsyncSession, turn: CanonicalTurn
) -> tuple[AgentRun, CanonicalTurn, AgentResult | None] | None:
    if not turn.references.run_ids:
        return None
    run_row = await session.get(AgentRunModel, turn.references.run_ids[-1])
    if run_row is None:
        raise CanonicalInvocationConflict("Canonical Turn references a missing Run")
    result = None
    if turn.references.result_ids:
        result_row = await session.get(AgentResultModel, turn.references.result_ids[-1])
        if result_row is None:
            raise CanonicalInvocationConflict("Canonical Turn references a missing Result")
        result = _result_from_row(result_row)
    return _run_from_row(run_row), turn, result


def _validate_run_owner(run: AgentRun) -> None:
    if not all((run.request_id, run.session_id, run.tenant_id, run.user_id, run.agent_id)):
        raise CanonicalInvocationConflict("Canonical Run requires trusted ownership")


def _validate_start_turn(
    row: CanonicalTurnModel | CanonicalTurn | None, run: AgentRun
) -> CanonicalTurn:
    if row is None:
        raise CanonicalInvocationConflict("Canonical Turn not found")
    turn = _turn_from_row(row) if isinstance(row, CanonicalTurnModel) else row
    if (
        turn.tenant_id != run.tenant_id
        or turn.user_id != run.user_id
        or turn.session_id != run.session_id
        or turn.request_id != run.request_id
    ):
        raise CanonicalInvocationConflict("Canonical Turn ownership conflict")
    return turn


def _attach_run(run: AgentRun, turn: CanonicalTurn) -> tuple[AgentRun, CanonicalTurn]:
    if turn.status.is_terminal:
        raise CanonicalInvocationConflict("Canonical Turn is already terminal")
    now = datetime.now(UTC)
    attached_run = run.model_copy(
        update={"turn_id": turn.turn_id, "status": "running", "updated_at": now}
    )
    references = turn.references.model_copy(deep=True)
    if run.run_id not in references.run_ids:
        references.run_ids.append(run.run_id)
    return attached_run, turn.model_copy(
        update={
            "status": TurnStatus.RUNNING,
            "state_version": turn.state_version + 1,
            "references": references,
            "updated_at": now,
        }
    )


def _completion_bundle(
    *,
    run: AgentRun,
    result: AgentResult,
    turn: CanonicalTurn,
    response_text: str,
    eligibility: FormationEligibilitySnapshot,
) -> TurnCompletionBundle:
    if not run.turn_id or run.turn_id != turn.turn_id:
        raise CanonicalInvocationConflict("Run does not belong to Canonical Turn")
    if turn.status.is_terminal:
        raise CanonicalInvocationConflict("Canonical Turn is already terminal")
    if result.run_id != run.run_id:
        raise CanonicalInvocationConflict("Result does not belong to Run")
    now = datetime.now(UTC)
    terminal_status = TurnStatus.COMPLETED if result.status == "completed" else TurnStatus.FAILED
    terminal_run = run.model_copy(
        update={
            "status": result.status,
            "turn_id": turn.turn_id,
            "state_version": run.state_version + 1,
            "updated_at": now,
        }
    )
    terminal_result = result.model_copy(
        update={
            "turn_id": turn.turn_id,
            "run_state_version": terminal_run.state_version,
        }
    )
    references = turn.references.model_copy(deep=True)
    if terminal_run.run_id not in references.run_ids:
        references.run_ids.append(terminal_run.run_id)
    if terminal_result.result_id not in references.result_ids:
        references.result_ids.append(terminal_result.result_id)
    final_response = TurnSemanticResponse(
        kind="agent_result" if terminal_status == TurnStatus.COMPLETED else "error",
        text=response_text,
        output=terminal_result.output,
        error=terminal_result.error,
    )
    completed_turn = turn.model_copy(
        update={
            "status": terminal_status,
            "state_version": turn.state_version + 1,
            "references": references,
            "final_response": final_response,
            "updated_at": now,
            "completed_at": now,
        }
    )
    event_type = "turn.completed" if terminal_status == TurnStatus.COMPLETED else "turn.failed"
    outbox = TurnOutboxEvent(
        outbox_id=f"outbox_{uuid4().hex}",
        turn_id=turn.turn_id,
        event_type=event_type,
        idempotency_key=f"{event_type}:{turn.turn_id}",
        payload={
            "turn_id": turn.turn_id,
            "tenant_id": turn.tenant_id,
            "user_id": turn.user_id,
            "request_id": turn.request_id,
            "run_id": terminal_run.run_id,
            "result_id": terminal_result.result_id,
            "state_version": completed_turn.state_version,
            "formation_eligibility": eligibility.model_dump(mode="json"),
        },
        available_at=now,
    )
    return TurnCompletionBundle(
        run=terminal_run,
        result=terminal_result,
        turn=completed_turn,
        outbox=outbox,
    )


def _apply_active_turn(row: CanonicalTurnModel, turn: CanonicalTurn) -> None:
    from app.repositories.json_utils import dumps

    row.status = turn.status.value
    row.state_version = turn.state_version
    row.references_text = dumps(turn.references.model_dump(mode="json"))
    row.updated_at = turn.updated_at
