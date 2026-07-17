import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    AgentEventModel,
    AgentResultModel,
    AgentRunModel,
    CanonicalTurnModel,
    PlanModel,
    PlanStepModel,
    TurnOutboxModel,
)
from app.repositories.database import (
    DatabasePlanRepository,
    _result_from_row,
    _run_from_row,
    _run_values,
)
from app.repositories.json_utils import dumps, loads
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turn_transactions import (
    DatabaseTurnTransactionCoordinator,
    TurnCompletionBundle,
)
from app.repositories.turns import MemoryTurnRepository, _turn_from_row
from app.schemas.delegated_runs import (
    DelegatedRunCompleteCommand,
    DelegatedRunOrphanQuery,
    DelegatedRunProgressCommand,
    DelegatedRunStartCommand,
    DelegatedRunTimeoutCommand,
)
from app.schemas.events import AgentEvent
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.plans import Plan
from app.schemas.turns import CanonicalTurn, TurnOutboxEvent, TurnSemanticResponse, TurnStatus
from app.services.delegated_run_service import (
    DelegatedRunCompletionResult,
    DelegatedRunCompletionStore,
    DelegatedRunMaintenanceStore,
    DelegatedRunProgressStore,
    DelegatedRunStartResult,
    DelegatedRunStartStore,
)


class DelegatedRunStartConflict(ValueError):
    pass


class DatabaseDelegatedRunStartStore(DelegatedRunStartStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def start(
        self,
        *,
        command: DelegatedRunStartCommand,
        run_id: str,
        delegation_key: str,
    ) -> DelegatedRunStartResult:
        try:
            async with self.session_factory() as session, session.begin():
                existing = await session.scalar(
                    select(AgentRunModel).where(AgentRunModel.delegation_key == delegation_key)
                )
                if existing is not None:
                    turn = await session.get(CanonicalTurnModel, existing.turn_id)
                    return DelegatedRunStartResult(
                        run=_run_from_row(existing),
                        turn=_turn_from_row(turn),
                        duplicate=True,
                    )
                turn_row = await session.scalar(
                    select(CanonicalTurnModel)
                    .where(CanonicalTurnModel.turn_id == command.turn_id)
                    .with_for_update()
                )
                turn = _validate_start_turn(turn_row, command)
                await _validate_plan_step(session, command)
                run = _new_run(command, run_id=run_id, delegation_key=delegation_key)
                session.add(AgentRunModel(**_run_values(run)))
                updated_turn = _attach_run(turn, run)
                _apply_turn_row(turn_row, updated_turn)
                await session.flush()
                return DelegatedRunStartResult(
                    run=run,
                    turn=updated_turn,
                    duplicate=False,
                )
        except IntegrityError:
            async with self.session_factory() as session:
                existing = await session.scalar(
                    select(AgentRunModel).where(AgentRunModel.delegation_key == delegation_key)
                )
                if existing is None:
                    raise
                turn = await session.get(CanonicalTurnModel, existing.turn_id)
                return DelegatedRunStartResult(
                    run=_run_from_row(existing),
                    turn=_turn_from_row(turn),
                    duplicate=True,
                )


class MemoryDelegatedRunStartStore(DelegatedRunStartStore):
    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        turn_repository: MemoryTurnRepository,
    ) -> None:
        self.run_repository = run_repository
        self.turn_repository = turn_repository
        self.delegation_keys: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        command: DelegatedRunStartCommand,
        run_id: str,
        delegation_key: str,
    ) -> DelegatedRunStartResult:
        async with self._lock:
            existing_id = self.delegation_keys.get(delegation_key)
            if existing_id:
                existing = await self.run_repository.get_run(existing_id)
                turn = await self.turn_repository.get(
                    command.turn_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
                return DelegatedRunStartResult(existing, turn, True)
            turn = await self.turn_repository.get(
                command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            turn = _validate_start_turn(turn, command)
            run = _new_run(command, run_id=run_id, delegation_key=delegation_key)
            updated_turn = _attach_run(turn, run)
            snapshot = turn.model_copy(deep=True)
            await self.run_repository.add_run(run)
            try:
                stored_turn = await self.turn_repository.update_if_version(
                    updated_turn,
                    expected_version=turn.state_version,
                )
                if stored_turn is None:
                    raise DelegatedRunStartConflict("Turn changed concurrently")
            except Exception:
                self.run_repository.runs.pop(run.run_id, None)
                self.turn_repository.turns[turn.turn_id] = snapshot
                raise
            self.delegation_keys[delegation_key] = run.run_id
            return DelegatedRunStartResult(run, stored_turn, False)


class DatabaseDelegatedRunProgressStore(DelegatedRunProgressStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def progress(self, command: DelegatedRunProgressCommand) -> tuple[AgentRun, bool]:
        async with self.session_factory() as session, session.begin():
            existing_event = await session.get(AgentEventModel, command.event_id)
            if existing_event is not None:
                _validate_duplicate_event(existing_event, command)
                run = await session.get(AgentRunModel, command.run_id)
                return _run_from_row(run), True
            row = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == command.run_id)
                .with_for_update()
            )
            run = _validate_progress_run(row, command)
            row.status = command.status
            row.state_version = run.state_version + 1
            row.event_sequence = command.sequence
            row.heartbeat_at = command.occurred_at
            row.updated_at = command.occurred_at
            session.add(AgentEventModel(**_progress_event_values(command, run, row.state_version)))
            await session.flush()
            return _run_from_row(row), False


class MemoryDelegatedRunProgressStore(DelegatedRunProgressStore):
    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        event_repository: MemoryEventRepository,
    ) -> None:
        self.run_repository = run_repository
        self.event_repository = event_repository
        self._lock = asyncio.Lock()

    async def progress(self, command: DelegatedRunProgressCommand) -> tuple[AgentRun, bool]:
        async with self._lock:
            existing = await self.event_repository.get_event(
                command.event_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if existing is not None:
                _validate_duplicate_event(existing, command)
                run = await self.run_repository.get_run(command.run_id)
                return run, True
            run = await self.run_repository.get_run(command.run_id)
            run = _validate_progress_run(run, command)
            updated = run.model_copy(
                update={
                    "status": command.status,
                    "state_version": run.state_version + 1,
                    "event_sequence": command.sequence,
                    "heartbeat_at": command.occurred_at,
                    "updated_at": command.occurred_at,
                }
            )
            await self.run_repository.update_run(updated)
            event = AgentEvent(
                event_id=command.event_id,
                run_id=command.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                agent_id=command.agent_id,
                user_id=command.user_id,
                tenant_id=command.tenant_id,
                turn_id=command.turn_id,
                event_type="agent_progress",
                status=command.status,
                plan_id=command.plan_id,
                step_id=command.step_id,
                sequence=command.sequence,
                run_state_version=updated.state_version,
                payload=command.payload,
                created_at=command.occurred_at,
            )
            await self.event_repository.add_agent_event(event)
            return updated, False


class DatabaseDelegatedRunCompletionStore(DelegatedRunCompletionStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.coordinator = DatabaseTurnTransactionCoordinator(session_factory)

    async def complete(self, command: DelegatedRunCompleteCommand) -> DelegatedRunCompletionResult:
        duplicate = await self._duplicate(command)
        if duplicate is not None:
            return duplicate
        async with self.session_factory() as session:
            run_row = await session.get(AgentRunModel, command.run_id)
            turn_row = await session.get(CanonicalTurnModel, command.turn_id)
        run = _validate_final_run(run_row, command)
        if turn_row is None:
            raise DelegatedRunStartConflict("Canonical Turn not found")
        turn = _turn_from_row(turn_row)
        plan = None
        if command.plan_id:
            plan = await DatabasePlanRepository(self.session_factory).get(
                command.plan_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if plan is None:
                raise DelegatedRunStartConflict("Plan not found")
        bundle = _completion_bundle(command, run=run, turn=turn, plan=plan)
        try:
            await self.coordinator.complete(bundle)
        except IntegrityError:
            duplicate = await self._duplicate(command)
            if duplicate is not None:
                return duplicate
            raise
        return DelegatedRunCompletionResult(
            run=bundle.run,
            result=bundle.result,
            turn=bundle.turn,
            duplicate=False,
        )

    async def _duplicate(
        self, command: DelegatedRunCompleteCommand
    ) -> DelegatedRunCompletionResult | None:
        async with self.session_factory() as session:
            event = await session.get(AgentEventModel, command.event_id)
            if event is None:
                return None
            _validate_final_event_identity(event, command)
            result_id = loads(event.payload_text, {}).get("result_id")
            run = await session.get(AgentRunModel, command.run_id)
            result = await session.get(AgentResultModel, result_id) if result_id else None
            turn = await session.get(CanonicalTurnModel, command.turn_id)
            if run is None or result is None or turn is None:
                raise DelegatedRunStartConflict("Final Event has incomplete canonical state")
            return DelegatedRunCompletionResult(
                run=_run_from_row(run),
                result=_result_from_row(result),
                turn=_turn_from_row(turn),
                duplicate=True,
            )


class MemoryDelegatedRunCompletionStore(DelegatedRunCompletionStore):
    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        result_repository: MemoryResultRepository,
        event_repository: MemoryEventRepository,
        turn_repository: MemoryTurnRepository,
        outbox_repository: MemoryTurnOutboxRepository,
        plan_repository: MemoryPlanRepository | None = None,
    ) -> None:
        self.run_repository = run_repository
        self.result_repository = result_repository
        self.event_repository = event_repository
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        self.plan_repository = plan_repository
        self._lock = asyncio.Lock()

    async def complete(self, command: DelegatedRunCompleteCommand) -> DelegatedRunCompletionResult:
        async with self._lock:
            existing_event = await self.event_repository.get_event(
                command.event_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if existing_event is not None:
                _validate_final_event_identity(existing_event, command)
                result_id = existing_event.payload.get("result_id")
                result = next(
                    item for item in self.result_repository.results if item.result_id == result_id
                )
                run = await self.run_repository.get_run(command.run_id)
                turn = await self.turn_repository.get(
                    command.turn_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
                return DelegatedRunCompletionResult(run, result, turn, True)
            run = _validate_final_run(await self.run_repository.get_run(command.run_id), command)
            turn = await self.turn_repository.get(
                command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if turn is None:
                raise DelegatedRunStartConflict("Canonical Turn not found")
            plan = None
            if command.plan_id:
                if self.plan_repository is None:
                    raise DelegatedRunStartConflict("Plan repository is not configured")
                plan = await self.plan_repository.get(
                    command.plan_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
            bundle = _completion_bundle(command, run=run, turn=turn, plan=plan)
            snapshots = (
                run.model_copy(deep=True),
                turn.model_copy(deep=True),
                list(self.result_repository.results),
                dict(self.event_repository.agent_events),
                dict(self.outbox_repository.events),
            )
            try:
                await self.run_repository.update_run(bundle.run)
                await self.result_repository.add_result(bundle.result)
                if bundle.plan is not None and self.plan_repository is not None:
                    await self.plan_repository.save(bundle.plan)
                stored_turn = await self.turn_repository.update_if_version(
                    bundle.turn,
                    expected_version=turn.state_version,
                )
                if stored_turn is None:
                    raise DelegatedRunStartConflict("Turn changed concurrently")
                await self.event_repository.add_agent_event(bundle.event)
                await self.outbox_repository.add_idempotent(bundle.outbox)
            except Exception:
                old_run, old_turn, results, events, outbox = snapshots
                self.run_repository.runs[old_run.run_id] = old_run
                self.turn_repository.turns[old_turn.turn_id] = old_turn
                self.result_repository.results = results
                self.event_repository.agent_events = events
                self.outbox_repository.events = outbox
                raise
            return DelegatedRunCompletionResult(bundle.run, bundle.result, bundle.turn, False)


class DatabaseDelegatedRunMaintenanceStore(DelegatedRunMaintenanceStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def list_orphans(self, query: DelegatedRunOrphanQuery) -> list[AgentRun]:
        async with self.session_factory() as session:
            conditions = [
                AgentRunModel.delegated.is_(True),
                AgentRunModel.status.in_(["pending", "running", "blocked"]),
                or_(
                    AgentRunModel.deadline_at <= query.now,
                    AgentRunModel.heartbeat_at <= query.stale_before,
                    and_(
                        AgentRunModel.heartbeat_at.is_(None),
                        AgentRunModel.updated_at <= query.stale_before,
                    ),
                ),
            ]
            if query.tenant_id:
                conditions.append(AgentRunModel.tenant_id == query.tenant_id)
            rows = (
                (
                    await session.execute(
                        select(AgentRunModel)
                        .where(*conditions)
                        .order_by(AgentRunModel.deadline_at, AgentRunModel.updated_at)
                        .limit(query.limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_run_from_row(row) for row in rows]

    async def timeout(self, command: DelegatedRunTimeoutCommand) -> tuple[AgentRun, bool]:
        async with self.session_factory() as session, session.begin():
            existing_event = await session.get(AgentEventModel, command.event_id)
            if existing_event is not None:
                _validate_maintenance_event(existing_event, command)
                run = await session.get(AgentRunModel, command.run_id)
                return _run_from_row(run), True
            run_row = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == command.run_id)
                .with_for_update()
            )
            run = _validate_timeout_run(run_row, command)
            turn_row = await session.scalar(
                select(CanonicalTurnModel)
                .where(CanonicalTurnModel.turn_id == command.turn_id)
                .with_for_update()
            )
            if (
                turn_row is None
                or turn_row.tenant_id != command.tenant_id
                or turn_row.user_id != command.user_id
            ):
                raise DelegatedRunStartConflict("Timeout Turn ownership conflict")
            timeout_at = max(
                _as_utc(command.occurred_at),
                _as_utc(turn_row.created_at),
            )
            run_row.status = "timed_out"
            run_row.state_version = run.state_version + 1
            run_row.event_sequence = run.event_sequence + 1
            run_row.terminal_event_id = command.event_id
            run_row.heartbeat_at = timeout_at
            run_row.updated_at = timeout_at
            turn_row.status = "timed_out"
            turn_row.state_version += 1
            turn_row.updated_at = timeout_at
            turn_row.completed_at = timeout_at
            await _fail_plan_step(session, run, command)
            session.add(AgentEventModel(**_timeout_event_values(run, command)))
            session.add(
                TurnOutboxModel(
                    outbox_id=f"outbox_{uuid4().hex}",
                    turn_id=command.turn_id,
                    event_type="turn.timed_out",
                    idempotency_key=f"turn.timed_out:{command.turn_id}",
                    payload_text=dumps({"turn_id": command.turn_id, "run_id": command.run_id}),
                    status="pending",
                    attempt_count=0,
                    max_attempts=5,
                    available_at=command.occurred_at,
                )
            )
            await session.flush()
            return _run_from_row(run_row), False


class MemoryDelegatedRunMaintenanceStore(DelegatedRunMaintenanceStore):
    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        event_repository: MemoryEventRepository,
        turn_repository: MemoryTurnRepository,
        outbox_repository: MemoryTurnOutboxRepository,
    ) -> None:
        self.run_repository = run_repository
        self.event_repository = event_repository
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        self._lock = asyncio.Lock()

    async def list_orphans(self, query: DelegatedRunOrphanQuery) -> list[AgentRun]:
        runs = []
        for run in self.run_repository.runs.values():
            if query.tenant_id and run.tenant_id != query.tenant_id:
                continue
            activity = run.heartbeat_at or run.updated_at
            stale = (run.deadline_at is not None and run.deadline_at <= query.now) or (
                activity is not None and activity <= query.stale_before
            )
            if run.delegated and run.status in {"pending", "running", "blocked"} and stale:
                runs.append(run.model_copy(deep=True))
        return runs[: query.limit]

    async def timeout(self, command: DelegatedRunTimeoutCommand) -> tuple[AgentRun, bool]:
        async with self._lock:
            existing = await self.event_repository.get_event(
                command.event_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if existing is not None:
                _validate_maintenance_event(existing, command)
                return await self.run_repository.get_run(command.run_id), True
            run = _validate_timeout_run(await self.run_repository.get_run(command.run_id), command)
            turn = await self.turn_repository.get(
                command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if turn is None:
                raise DelegatedRunStartConflict("Timeout Turn ownership conflict")
            updated_run = run.model_copy(
                update={
                    "status": "timed_out",
                    "state_version": run.state_version + 1,
                    "event_sequence": run.event_sequence + 1,
                    "terminal_event_id": command.event_id,
                    "heartbeat_at": command.occurred_at,
                    "updated_at": command.occurred_at,
                }
            )
            updated_turn = turn.model_copy(
                update={
                    "status": TurnStatus.TIMED_OUT,
                    "state_version": turn.state_version + 1,
                    "updated_at": command.occurred_at,
                    "completed_at": command.occurred_at,
                }
            )
            await self.run_repository.update_run(updated_run)
            stored_turn = await self.turn_repository.update_if_version(
                updated_turn,
                expected_version=turn.state_version,
            )
            if stored_turn is None:
                raise DelegatedRunStartConflict("Timeout Turn changed concurrently")
            event = AgentEvent(
                event_id=command.event_id,
                run_id=command.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                agent_id=run.agent_id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                turn_id=run.turn_id,
                event_type="agent_error",
                status="timed_out",
                plan_id=run.plan_id,
                step_id=run.step_id,
                sequence=updated_run.event_sequence,
                run_state_version=updated_run.state_version,
                payload={"reason": command.reason},
                created_at=command.occurred_at,
            )
            await self.event_repository.add_agent_event(event)
            await self.outbox_repository.add_idempotent(
                TurnOutboxEvent(
                    outbox_id=f"outbox_{uuid4().hex}",
                    turn_id=command.turn_id,
                    event_type="turn.timed_out",
                    idempotency_key=f"turn.timed_out:{command.turn_id}",
                    payload={"turn_id": command.turn_id, "run_id": command.run_id},
                    available_at=command.occurred_at,
                )
            )
            return updated_run, False


def _validate_start_turn(
    turn: CanonicalTurnModel | CanonicalTurn | None,
    command: DelegatedRunStartCommand,
) -> CanonicalTurn:
    if turn is None:
        raise DelegatedRunStartConflict("Canonical Turn not found")
    canonical = _turn_from_row(turn) if isinstance(turn, CanonicalTurnModel) else turn
    if (
        canonical.tenant_id != command.tenant_id
        or canonical.user_id != command.user_id
        or canonical.session_id != command.session_id
        or canonical.request_id != command.request_id
        or canonical.status.is_terminal
    ):
        raise DelegatedRunStartConflict("Canonical Turn ownership or state conflict")
    return canonical


async def _validate_plan_step(session: AsyncSession, command: DelegatedRunStartCommand) -> None:
    if command.plan_id is None and command.step_id is None:
        return
    if not command.plan_id or not command.step_id:
        raise DelegatedRunStartConflict("Plan and Step must be supplied together")
    plan = await session.scalar(
        select(PlanModel).where(
            PlanModel.plan_id == command.plan_id,
            PlanModel.tenant_id == command.tenant_id,
            PlanModel.user_id == command.user_id,
        )
    )
    step = await session.scalar(
        select(PlanStepModel).where(
            PlanStepModel.plan_id == command.plan_id,
            PlanStepModel.step_id == command.step_id,
            PlanStepModel.agent_id == command.agent_id,
        )
    )
    if plan is None or step is None:
        raise DelegatedRunStartConflict("Plan Step ownership conflict")


def _new_run(command: DelegatedRunStartCommand, *, run_id: str, delegation_key: str) -> AgentRun:
    now = datetime.now(UTC)
    return AgentRun(
        run_id=run_id,
        request_id=command.request_id,
        session_id=command.session_id,
        agent_id=command.agent_id,
        user_id=command.user_id,
        tenant_id=command.tenant_id,
        turn_id=command.turn_id,
        plan_id=command.plan_id,
        step_id=command.step_id,
        status="pending",
        invoker_type="delegated",
        delegated=True,
        delegation_key=delegation_key,
        state_version=1,
        deadline_at=command.deadline_at,
        input=command.input,
        created_at=now,
        updated_at=now,
    )


def _attach_run(turn: CanonicalTurn, run: AgentRun) -> CanonicalTurn:
    references = turn.references.model_copy(deep=True)
    references.run_ids.append(run.run_id)
    if run.plan_id:
        if references.plan_id and references.plan_id != run.plan_id:
            raise DelegatedRunStartConflict("Turn Plan reference conflict")
        references.plan_id = run.plan_id
    return turn.model_copy(
        update={
            "status": TurnStatus.RUNNING,
            "state_version": turn.state_version + 1,
            "references": references,
            "updated_at": datetime.now(UTC),
        }
    )


def _apply_turn_row(row: CanonicalTurnModel, turn: CanonicalTurn) -> None:
    row.status = turn.status.value
    row.state_version = turn.state_version
    row.references_text = dumps(turn.references.model_dump(mode="json"))
    row.updated_at = turn.updated_at


def _validate_progress_run(
    run: AgentRunModel | AgentRun | None,
    command: DelegatedRunProgressCommand,
) -> AgentRun:
    if run is None:
        raise DelegatedRunStartConflict("Delegated Run not found")
    canonical = _run_from_row(run) if isinstance(run, AgentRunModel) else run
    if (
        not canonical.delegated
        or canonical.status in {"completed", "failed", "cancelled", "timed_out"}
        or canonical.turn_id != command.turn_id
        or canonical.tenant_id != command.tenant_id
        or canonical.user_id != command.user_id
        or canonical.agent_id != command.agent_id
        or canonical.plan_id != command.plan_id
        or canonical.step_id != command.step_id
        or canonical.state_version != command.expected_state_version
        or command.sequence <= canonical.event_sequence
    ):
        raise DelegatedRunStartConflict("Delegated Run progress identity/order conflict")
    return canonical


def _progress_event_values(
    command: DelegatedRunProgressCommand,
    run: AgentRun,
    run_state_version: int,
) -> dict:
    return {
        "event_id": command.event_id,
        "run_id": command.run_id,
        "request_id": run.request_id,
        "session_id": run.session_id,
        "agent_id": command.agent_id,
        "user_id": command.user_id,
        "tenant_id": command.tenant_id,
        "turn_id": command.turn_id,
        "agent_session_id": None,
        "event_type": "agent_progress",
        "status": command.status,
        "plan_id": command.plan_id,
        "step_id": command.step_id,
        "sequence": command.sequence,
        "run_state_version": run_state_version,
        "payload_text": dumps(command.payload),
        "created_at": command.occurred_at,
    }


def _validate_duplicate_event(
    event: AgentEventModel | AgentEvent, command: DelegatedRunProgressCommand
) -> None:
    values = {
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "tenant_id": event.tenant_id,
        "user_id": event.user_id,
        "agent_id": event.agent_id,
        "plan_id": event.plan_id,
        "step_id": event.step_id,
        "sequence": event.sequence,
    }
    expected = {
        "run_id": command.run_id,
        "turn_id": command.turn_id,
        "tenant_id": command.tenant_id,
        "user_id": command.user_id,
        "agent_id": command.agent_id,
        "plan_id": command.plan_id,
        "step_id": command.step_id,
        "sequence": command.sequence,
    }
    if values != expected:
        raise DelegatedRunStartConflict("Event idempotency identity conflict")


def _validate_final_run(
    run: AgentRunModel | AgentRun | None,
    command: DelegatedRunCompleteCommand,
) -> AgentRun:
    if run is None:
        raise DelegatedRunStartConflict("Delegated Run not found")
    canonical = _run_from_row(run) if isinstance(run, AgentRunModel) else run
    if (
        not canonical.delegated
        or canonical.status in {"completed", "failed", "cancelled", "timed_out"}
        or canonical.turn_id != command.turn_id
        or canonical.tenant_id != command.tenant_id
        or canonical.user_id != command.user_id
        or canonical.agent_id != command.agent_id
        or canonical.plan_id != command.plan_id
        or canonical.step_id != command.step_id
        or canonical.state_version != command.expected_state_version
    ):
        raise DelegatedRunStartConflict("Delegated Run final identity/state conflict")
    return canonical


def _completion_bundle(
    command: DelegatedRunCompleteCommand,
    *,
    run: AgentRun,
    turn: CanonicalTurn,
    plan: Plan | None,
) -> TurnCompletionBundle:
    now = command.occurred_at
    updated_run = run.model_copy(
        update={
            "status": "completed",
            "state_version": run.state_version + 1,
            "event_sequence": run.event_sequence + 1,
            "terminal_event_id": command.event_id,
            "heartbeat_at": now,
            "output": command.output,
            "updated_at": now,
        }
    )
    result = AgentResult(
        result_id=command.result_id,
        run_id=run.run_id,
        session_id=run.session_id,
        agent_id=run.agent_id,
        user_id=run.user_id,
        tenant_id=run.tenant_id,
        turn_id=run.turn_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
        status="completed",
        run_state_version=updated_run.state_version,
        message=command.response_text,
        output=command.output,
        artifact_refs=command.artifact_refs,
        created_at=now,
    )
    references = turn.references.model_copy(deep=True)
    if command.result_id not in references.result_ids:
        references.result_ids.append(command.result_id)
    completed_turn = turn.model_copy(
        update={
            "status": TurnStatus.COMPLETED,
            "state_version": turn.state_version + 1,
            "references": references,
            "final_response": TurnSemanticResponse(
                kind="agent_result",
                text=command.response_text,
                output=command.output,
            ),
            "updated_at": now,
            "completed_at": now,
        }
    )
    updated_plan = _complete_plan_step(plan, command) if plan else None
    event = AgentEvent(
        event_id=command.event_id,
        run_id=run.run_id,
        request_id=run.request_id,
        session_id=run.session_id,
        agent_id=run.agent_id,
        user_id=run.user_id,
        tenant_id=run.tenant_id,
        turn_id=run.turn_id,
        event_type="agent_result",
        status="completed",
        plan_id=run.plan_id,
        step_id=run.step_id,
        sequence=updated_run.event_sequence,
        run_state_version=updated_run.state_version,
        payload={"result_id": command.result_id},
        created_at=now,
    )
    outbox = TurnOutboxEvent(
        outbox_id=f"outbox_{uuid4().hex}",
        turn_id=turn.turn_id,
        event_type="turn.completed",
        idempotency_key=f"turn.completed:{turn.turn_id}",
        payload={
            "turn_id": turn.turn_id,
            "run_id": run.run_id,
            "result_id": command.result_id,
            "plan_id": run.plan_id,
        },
        available_at=now,
    )
    return TurnCompletionBundle(
        run=updated_run,
        result=result,
        turn=completed_turn,
        outbox=outbox,
        plan=updated_plan,
        event=event,
    )


def _complete_plan_step(plan: Plan, command: DelegatedRunCompleteCommand) -> Plan:
    if not command.step_id:
        raise DelegatedRunStartConflict("Plan completion requires Step")
    matched = False
    steps = []
    for step in plan.steps:
        if step.step_id == command.step_id and step.agent_id == command.agent_id:
            steps.append(step.model_copy(update={"status": "completed"}))
            matched = True
        else:
            steps.append(step)
    if not matched:
        raise DelegatedRunStartConflict("Plan Step association conflict")
    remaining = next((step.step_id for step in steps if step.status == "pending"), None)
    return plan.model_copy(
        update={
            "steps": steps,
            "status": "completed" if remaining is None else "running",
            "current_step_id": remaining,
            "state_version": plan.state_version + 1,
            "last_event_id": command.event_id,
            "updated_at": command.occurred_at,
            "formation_event_type": "update",
        }
    )


def _validate_final_event_identity(
    event: AgentEventModel | AgentEvent,
    command: DelegatedRunCompleteCommand,
) -> None:
    if (
        event.run_id != command.run_id
        or event.turn_id != command.turn_id
        or event.tenant_id != command.tenant_id
        or event.user_id != command.user_id
        or event.agent_id != command.agent_id
        or event.plan_id != command.plan_id
        or event.step_id != command.step_id
    ):
        raise DelegatedRunStartConflict("Final Event idempotency identity conflict")


def _validate_timeout_run(
    run: AgentRunModel | AgentRun | None,
    command: DelegatedRunTimeoutCommand,
) -> AgentRun:
    if run is None:
        raise DelegatedRunStartConflict("Delegated Run not found")
    canonical = _run_from_row(run) if isinstance(run, AgentRunModel) else run
    if (
        not canonical.delegated
        or canonical.status in {"completed", "failed", "cancelled", "timed_out"}
        or canonical.turn_id != command.turn_id
        or canonical.tenant_id != command.tenant_id
        or canonical.user_id != command.user_id
        or canonical.agent_id != command.agent_id
        or canonical.plan_id != command.plan_id
        or canonical.step_id != command.step_id
        or canonical.state_version != command.expected_state_version
        or canonical.deadline_at is None
        or _as_utc(command.occurred_at) < _as_utc(canonical.deadline_at)
    ):
        raise DelegatedRunStartConflict("Delegated Run timeout identity/deadline conflict")
    return canonical


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _fail_plan_step(
    session: AsyncSession,
    run: AgentRun,
    command: DelegatedRunTimeoutCommand,
) -> None:
    if not run.plan_id or not run.step_id:
        return
    plan = await session.scalar(
        select(PlanModel).where(PlanModel.plan_id == run.plan_id).with_for_update()
    )
    step = await session.scalar(
        select(PlanStepModel).where(
            PlanStepModel.plan_id == run.plan_id,
            PlanStepModel.step_id == run.step_id,
            PlanStepModel.agent_id == run.agent_id,
        )
    )
    if plan is None or step is None:
        raise DelegatedRunStartConflict("Timeout Plan Step association conflict")
    step.status = "failed"
    plan.status = "failed"
    plan.current_step_id = None
    plan.state_version += 1
    plan.updated_at = command.occurred_at


def _timeout_event_values(run: AgentRun, command: DelegatedRunTimeoutCommand) -> dict:
    return {
        "event_id": command.event_id,
        "run_id": command.run_id,
        "request_id": run.request_id,
        "session_id": run.session_id,
        "agent_id": run.agent_id,
        "user_id": run.user_id,
        "tenant_id": run.tenant_id,
        "turn_id": run.turn_id,
        "agent_session_id": None,
        "event_type": "agent_error",
        "status": "timed_out",
        "plan_id": run.plan_id,
        "step_id": run.step_id,
        "sequence": run.event_sequence + 1,
        "run_state_version": run.state_version + 1,
        "payload_text": dumps({"reason": command.reason}),
        "created_at": command.occurred_at,
    }


def _validate_maintenance_event(
    event: AgentEventModel | AgentEvent,
    command: DelegatedRunTimeoutCommand,
) -> None:
    if (
        event.run_id != command.run_id
        or event.turn_id != command.turn_id
        or event.tenant_id != command.tenant_id
        or event.user_id != command.user_id
        or event.agent_id != command.agent_id
        or event.plan_id != command.plan_id
        or event.step_id != command.step_id
    ):
        raise DelegatedRunStartConflict("Maintenance Event idempotency conflict")
