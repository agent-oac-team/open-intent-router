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
    _lock_plan_row,
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
    DELEGATED_RUN_MAINTENANCE_EVENT_PREFIX,
    DelegatedRunCancelCommand,
    DelegatedRunCompleteCommand,
    DelegatedRunEventCommand,
    DelegatedRunFailCommand,
    DelegatedRunOrphanQuery,
    DelegatedRunOverdueQuery,
    DelegatedRunProgressCommand,
    DelegatedRunStartCommand,
    DelegatedRunTimeoutCommand,
)
from app.schemas.events import AgentEvent
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.plans import Plan
from app.schemas.turns import CanonicalTurn, TurnOutboxEvent, TurnSemanticResponse, TurnStatus
from app.services.delegated_run_service import (
    DelegatedRunCancellationResult,
    DelegatedRunCancelStore,
    DelegatedRunCompletionResult,
    DelegatedRunCompletionStore,
    DelegatedRunFailureResult,
    DelegatedRunFailureStore,
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

    async def find_existing(self, *, delegation_key: str) -> AgentRun | None:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(AgentRunModel).where(AgentRunModel.delegation_key == delegation_key)
            )
            return _run_from_row(existing) if existing is not None else None

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
                run_values = _run_values(run)
                if command.handling_kind == "external_execution":
                    run_values["external_ticket_issuance_state"] = "unissued"
                session.add(AgentRunModel(**run_values))
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
        plan_repository: MemoryPlanRepository | None = None,
    ) -> None:
        self.run_repository = run_repository
        self.turn_repository = turn_repository
        self.plan_repository = plan_repository
        self.delegation_keys: dict[str, str] = {}
        self._lock = (
            plan_repository.delegated_run_lock if plan_repository is not None else asyncio.Lock()
        )

    async def find_existing(self, *, delegation_key: str) -> AgentRun | None:
        async with self._lock:
            existing_id = self.delegation_keys.get(delegation_key)
            if existing_id is None:
                existing_id = next(
                    (
                        run.run_id
                        for run in self.run_repository.runs.values()
                        if run.delegation_key == delegation_key
                    ),
                    None,
                )
                if existing_id is not None:
                    self.delegation_keys[delegation_key] = existing_id
            return await self.run_repository.get_run(existing_id) if existing_id else None

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
            await _validate_memory_plan_step(self.plan_repository, command)
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
        _validate_progress_event_namespace(command)
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
            await _project_database_clarification(session, command)
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
        plan_repository: MemoryPlanRepository | None = None,
    ) -> None:
        self.run_repository = run_repository
        self.event_repository = event_repository
        self.plan_repository = plan_repository
        self._lock = (
            plan_repository.delegated_run_lock if plan_repository is not None else asyncio.Lock()
        )

    async def progress(self, command: DelegatedRunProgressCommand) -> tuple[AgentRun, bool]:
        _validate_progress_event_namespace(command)
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
            plan_snapshot, updated_plan = await _memory_clarification_transition(
                self.plan_repository,
                command,
            )
            updated = run.model_copy(
                update={
                    "status": command.status,
                    "state_version": run.state_version + 1,
                    "event_sequence": command.sequence,
                    "heartbeat_at": command.occurred_at,
                    "updated_at": command.occurred_at,
                }
            )
            event = AgentEvent(
                event_id=command.event_id,
                run_id=command.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                agent_id=command.agent_id,
                user_id=command.user_id,
                tenant_id=command.tenant_id,
                turn_id=command.turn_id,
                event_type=command.event_type,
                status=command.status,
                plan_id=command.plan_id,
                step_id=command.step_id,
                sequence=command.sequence,
                run_state_version=updated.state_version,
                payload=command.payload,
                created_at=command.occurred_at,
            )
            snapshots = (
                run.model_copy(deep=True),
                plan_snapshot,
                dict(self.event_repository.agent_events),
            )
            try:
                await self.run_repository.update_run(updated)
                if updated_plan is not None and self.plan_repository is not None:
                    await self.plan_repository.save(updated_plan)
                await self.event_repository.add_agent_event(event)
            except Exception:
                old_run, old_plan, events = snapshots
                self.run_repository.runs[old_run.run_id] = old_run
                if old_plan is not None and self.plan_repository is not None:
                    self.plan_repository.plans[old_plan.plan_id] = old_plan
                self.event_repository.agent_events = events
                raise
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
            _validate_completion_event_identity(event, command)
            result_id = loads(event.payload_text, {}).get("result_id")
            run = await session.get(AgentRunModel, command.run_id)
            result = await session.get(AgentResultModel, result_id) if result_id else None
            turn = await session.get(CanonicalTurnModel, command.turn_id)
            if run is None or result is None or turn is None:
                raise DelegatedRunStartConflict("Final Event has incomplete canonical state")
            canonical_result = _result_from_row(result)
            _validate_completion_result_identity(canonical_result, command)
            return DelegatedRunCompletionResult(
                run=_run_from_row(run),
                result=canonical_result,
                turn=_turn_from_row(turn),
                duplicate=True,
            )


class DatabaseDelegatedRunFailureStore(DelegatedRunFailureStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def fail(self, command: DelegatedRunFailCommand) -> DelegatedRunFailureResult:
        try:
            async with self.session_factory() as session, session.begin():
                existing_event = await session.get(AgentEventModel, command.event_id)
                if existing_event is not None:
                    return await self._duplicate(session, existing_event, command)

                run_row = await session.scalar(
                    select(AgentRunModel)
                    .where(AgentRunModel.run_id == command.run_id)
                    .with_for_update()
                )
                run = _validate_failure_run(run_row, command)
                turn_row = await session.scalar(
                    select(CanonicalTurnModel)
                    .where(CanonicalTurnModel.turn_id == command.turn_id)
                    .with_for_update()
                )
                turn = _validate_failure_turn(turn_row, command)
                failed_at = max(_as_utc(command.occurred_at), _as_utc(turn.created_at))
                stored_failed_at = (
                    failed_at.replace(tzinfo=None)
                    if turn_row.created_at.tzinfo is None
                    else failed_at
                )

                run_row.status = "failed"
                run_row.state_version = run.state_version + 1
                run_row.event_sequence = run.event_sequence + 1
                run_row.terminal_event_id = command.event_id
                run_row.heartbeat_at = stored_failed_at
                run_row.error_text = dumps(command.error)
                run_row.updated_at = stored_failed_at

                turn_row.status = TurnStatus.FAILED.value
                turn_row.state_version = turn.state_version + 1
                turn_row.final_response_text = dumps(
                    TurnSemanticResponse(
                        kind="error",
                        text="Delegated agent execution failed",
                        error=command.error,
                    ).model_dump(mode="json")
                )
                turn_row.updated_at = stored_failed_at
                turn_row.completed_at = stored_failed_at

                await _fail_plan_step(session, run, command)
                session.add(
                    AgentEventModel(**_failure_event_values(run, command, stored_failed_at))
                )
                session.add(
                    TurnOutboxModel(
                        outbox_id=f"outbox_{uuid4().hex}",
                        turn_id=command.turn_id,
                        event_type="turn.failed",
                        idempotency_key=f"turn.failed:{command.turn_id}",
                        payload_text=dumps(
                            {
                                "turn_id": command.turn_id,
                                "run_id": command.run_id,
                                "state_version": turn_row.state_version,
                            }
                        ),
                        status="pending",
                        attempt_count=0,
                        max_attempts=5,
                        available_at=stored_failed_at,
                    )
                )
                await session.flush()
                return DelegatedRunFailureResult(
                    run=_run_from_row(run_row),
                    turn=_turn_from_row(turn_row),
                    duplicate=False,
                )
        except IntegrityError:
            async with self.session_factory() as session:
                existing_event = await session.get(AgentEventModel, command.event_id)
                if existing_event is None:
                    raise
                return await self._duplicate(session, existing_event, command)

    async def _duplicate(
        self,
        session: AsyncSession,
        event: AgentEventModel,
        command: DelegatedRunFailCommand,
    ) -> DelegatedRunFailureResult:
        _validate_failure_event_identity(event, command)
        run = await session.get(AgentRunModel, command.run_id)
        turn = await session.get(CanonicalTurnModel, command.turn_id)
        if run is None or turn is None:
            raise DelegatedRunStartConflict("Failure Event has incomplete canonical state")
        return DelegatedRunFailureResult(
            run=_run_from_row(run),
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
                _validate_completion_event_identity(existing_event, command)
                result_id = existing_event.payload.get("result_id")
                result = next(
                    item for item in self.result_repository.results if item.result_id == result_id
                )
                _validate_completion_result_identity(result, command)
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


class MemoryDelegatedRunFailureStore(DelegatedRunFailureStore):
    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        event_repository: MemoryEventRepository,
        turn_repository: MemoryTurnRepository,
        outbox_repository: MemoryTurnOutboxRepository,
        plan_repository: MemoryPlanRepository | None = None,
    ) -> None:
        self.run_repository = run_repository
        self.event_repository = event_repository
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        self.plan_repository = plan_repository
        self._lock = asyncio.Lock()

    async def fail(self, command: DelegatedRunFailCommand) -> DelegatedRunFailureResult:
        async with self._lock:
            existing_event = await self.event_repository.get_event(
                command.event_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if existing_event is not None:
                _validate_failure_event_identity(existing_event, command)
                run = await self.run_repository.get_run(command.run_id)
                turn = await self.turn_repository.get(
                    command.turn_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
                if turn is None:
                    raise DelegatedRunStartConflict("Failure Event has incomplete canonical state")
                return DelegatedRunFailureResult(run=run, turn=turn, duplicate=True)

            run = _validate_failure_run(await self.run_repository.get_run(command.run_id), command)
            turn = _validate_failure_turn(
                await self.turn_repository.get(
                    command.turn_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                ),
                command,
            )
            plan = None
            if command.plan_id:
                if self.plan_repository is None:
                    raise DelegatedRunStartConflict("Plan repository is not configured")
                plan = await self.plan_repository.get(
                    command.plan_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
                if plan is None:
                    raise DelegatedRunStartConflict("Plan not found")

            failed_at = max(_as_utc(command.occurred_at), _as_utc(turn.created_at))
            updated_run = run.model_copy(
                update={
                    "status": "failed",
                    "state_version": run.state_version + 1,
                    "event_sequence": run.event_sequence + 1,
                    "terminal_event_id": command.event_id,
                    "heartbeat_at": failed_at,
                    "error": command.error,
                    "updated_at": failed_at,
                }
            )
            updated_turn = turn.model_copy(
                update={
                    "status": TurnStatus.FAILED,
                    "state_version": turn.state_version + 1,
                    "final_response": TurnSemanticResponse(
                        kind="error",
                        text="Delegated agent execution failed",
                        error=command.error,
                    ),
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            )
            updated_plan = _failed_plan(plan, command, failed_at) if plan is not None else None
            event = AgentEvent(
                event_id=command.event_id,
                run_id=run.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                agent_id=run.agent_id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                turn_id=run.turn_id,
                event_type="agent_error",
                status="failed",
                plan_id=run.plan_id,
                step_id=run.step_id,
                sequence=updated_run.event_sequence,
                run_state_version=updated_run.state_version,
                payload={"error": command.error},
                created_at=failed_at,
            )
            outbox = TurnOutboxEvent(
                outbox_id=f"outbox_{uuid4().hex}",
                turn_id=turn.turn_id,
                event_type="turn.failed",
                idempotency_key=f"turn.failed:{turn.turn_id}",
                payload={
                    "turn_id": turn.turn_id,
                    "run_id": run.run_id,
                    "state_version": updated_turn.state_version,
                },
                available_at=failed_at,
            )
            snapshots = (
                run.model_copy(deep=True),
                turn.model_copy(deep=True),
                plan.model_copy(deep=True) if plan is not None else None,
                dict(self.event_repository.agent_events),
                dict(self.outbox_repository.events),
            )
            try:
                await self.run_repository.update_run(updated_run)
                if updated_plan is not None and self.plan_repository is not None:
                    await self.plan_repository.save(updated_plan)
                stored_turn = await self.turn_repository.update_if_version(
                    updated_turn,
                    expected_version=turn.state_version,
                )
                if stored_turn is None:
                    raise DelegatedRunStartConflict("Turn changed concurrently")
                await self.event_repository.add_agent_event(event)
                await self.outbox_repository.add_idempotent(outbox)
            except Exception:
                old_run, old_turn, old_plan, events, outbox_events = snapshots
                self.run_repository.runs[old_run.run_id] = old_run
                self.turn_repository.turns[old_turn.turn_id] = old_turn
                if old_plan is not None and self.plan_repository is not None:
                    self.plan_repository.plans[old_plan.plan_id] = old_plan
                self.event_repository.agent_events = events
                self.outbox_repository.events = outbox_events
                raise
            return DelegatedRunFailureResult(updated_run, stored_turn, False)


class DatabaseDelegatedRunCancelStore(DelegatedRunCancelStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def cancel(self, command: DelegatedRunCancelCommand) -> DelegatedRunCancellationResult:
        async with self.session_factory() as session, session.begin():
            existing_event = await session.get(AgentEventModel, command.event_id)
            if existing_event is not None:
                _validate_cancel_event(existing_event, command)
                run_row = await session.get(AgentRunModel, command.run_id)
                turn_row = await session.get(CanonicalTurnModel, command.turn_id)
                run, turn = _validate_cancelled_state(run_row, turn_row, command)
                return DelegatedRunCancellationResult(run, turn, True)

            run_row = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == command.run_id)
                .with_for_update()
            )
            run = _validate_cancel_run(run_row, command)
            turn_row = await session.scalar(
                select(CanonicalTurnModel)
                .where(CanonicalTurnModel.turn_id == command.turn_id)
                .with_for_update()
            )
            turn = _validate_cancel_turn(turn_row, command)
            cancelled_at = max(_as_utc(command.occurred_at), _as_utc(turn.created_at))
            updated_turn = turn.model_copy(
                update={
                    "status": TurnStatus.CANCELLED,
                    "state_version": turn.state_version + 1,
                    "updated_at": cancelled_at,
                    "completed_at": cancelled_at,
                }
            )

            run_row.status = "cancelled"
            run_row.state_version = run.state_version + 1
            run_row.event_sequence = run.event_sequence + 1
            run_row.terminal_event_id = command.event_id
            run_row.heartbeat_at = cancelled_at
            run_row.updated_at = cancelled_at
            turn_row.status = updated_turn.status.value
            turn_row.state_version = updated_turn.state_version
            turn_row.updated_at = updated_turn.updated_at
            turn_row.completed_at = updated_turn.completed_at
            await _cancel_plan_step(session, run, command, cancelled_at=cancelled_at)
            session.add(
                AgentEventModel(**_cancel_event_values(run, command, cancelled_at=cancelled_at))
            )
            session.add(
                TurnOutboxModel(
                    outbox_id=f"outbox_{uuid4().hex}",
                    turn_id=command.turn_id,
                    event_type="turn.cancelled",
                    idempotency_key=f"turn.cancelled:{command.turn_id}",
                    payload_text=dumps(
                        {
                            "turn_id": command.turn_id,
                            "run_id": command.run_id,
                            "reason": command.reason,
                        }
                    ),
                    status="pending",
                    attempt_count=0,
                    max_attempts=5,
                    available_at=cancelled_at,
                )
            )
            await session.flush()
            return DelegatedRunCancellationResult(
                _run_from_row(run_row),
                updated_turn,
                False,
            )


class MemoryDelegatedRunCancelStore(DelegatedRunCancelStore):
    def __init__(
        self,
        *,
        run_repository: MemoryRunRepository,
        event_repository: MemoryEventRepository,
        turn_repository: MemoryTurnRepository,
        outbox_repository: MemoryTurnOutboxRepository,
        plan_repository: MemoryPlanRepository | None = None,
    ) -> None:
        self.run_repository = run_repository
        self.event_repository = event_repository
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        self.plan_repository = plan_repository
        self._lock = asyncio.Lock()

    async def cancel(self, command: DelegatedRunCancelCommand) -> DelegatedRunCancellationResult:
        async with self._lock:
            existing_event = await self.event_repository.get_event(
                command.event_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if existing_event is not None:
                _validate_cancel_event(existing_event, command)
                run = await self.run_repository.get_run(command.run_id)
                turn = await self.turn_repository.get(
                    command.turn_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
                run, turn = _validate_cancelled_state(run, turn, command)
                return DelegatedRunCancellationResult(run, turn, True)

            run = _validate_cancel_run(
                await self.run_repository.get_run(command.run_id),
                command,
            )
            turn = _validate_cancel_turn(
                await self.turn_repository.get(
                    command.turn_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                ),
                command,
            )
            plan = None
            if command.plan_id:
                if self.plan_repository is None:
                    raise DelegatedRunStartConflict("Cancel Plan repository is not configured")
                plan = await self.plan_repository.get(
                    command.plan_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
                if plan is None:
                    raise DelegatedRunStartConflict("Cancel Plan not found")
            cancelled_at = max(_as_utc(command.occurred_at), _as_utc(turn.created_at))
            updated_run = run.model_copy(
                update={
                    "status": "cancelled",
                    "state_version": run.state_version + 1,
                    "event_sequence": run.event_sequence + 1,
                    "terminal_event_id": command.event_id,
                    "heartbeat_at": cancelled_at,
                    "updated_at": cancelled_at,
                }
            )
            updated_turn = turn.model_copy(
                update={
                    "status": TurnStatus.CANCELLED,
                    "state_version": turn.state_version + 1,
                    "updated_at": cancelled_at,
                    "completed_at": cancelled_at,
                }
            )
            updated_plan = (
                _cancelled_plan(plan, command, cancelled_at=cancelled_at)
                if plan is not None
                else None
            )
            event = AgentEvent(
                event_id=command.event_id,
                run_id=run.run_id,
                request_id=run.request_id,
                session_id=run.session_id,
                agent_id=run.agent_id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                turn_id=run.turn_id,
                event_type="agent_cancelled",
                status="cancelled",
                plan_id=run.plan_id,
                step_id=run.step_id,
                sequence=updated_run.event_sequence,
                run_state_version=updated_run.state_version,
                payload={"reason": command.reason},
                created_at=cancelled_at,
            )
            outbox = TurnOutboxEvent(
                outbox_id=f"outbox_{uuid4().hex}",
                turn_id=command.turn_id,
                event_type="turn.cancelled",
                idempotency_key=f"turn.cancelled:{command.turn_id}",
                payload={
                    "turn_id": command.turn_id,
                    "run_id": command.run_id,
                    "reason": command.reason,
                },
                available_at=cancelled_at,
            )
            snapshots = (
                run.model_copy(deep=True),
                turn.model_copy(deep=True),
                plan.model_copy(deep=True) if plan is not None else None,
                dict(self.event_repository.agent_events),
                dict(self.outbox_repository.events),
                dict(self.outbox_repository.idempotency_keys),
            )
            try:
                await self.run_repository.update_run(updated_run)
                if updated_plan is not None and self.plan_repository is not None:
                    await self.plan_repository.save(updated_plan)
                stored_turn = await self.turn_repository.update_if_version(
                    updated_turn,
                    expected_version=turn.state_version,
                )
                if stored_turn is None:
                    raise DelegatedRunStartConflict("Cancel Turn changed concurrently")
                await self.event_repository.add_agent_event(event)
                await self.outbox_repository.add_idempotent(outbox)
            except Exception:
                old_run, old_turn, old_plan, events, outbox_events, idempotency_keys = snapshots
                self.run_repository.runs[old_run.run_id] = old_run
                self.turn_repository.turns[old_turn.turn_id] = old_turn
                if old_plan is not None and self.plan_repository is not None:
                    self.plan_repository.plans[old_plan.plan_id] = old_plan
                self.event_repository.agent_events = events
                self.outbox_repository.events = outbox_events
                self.outbox_repository.idempotency_keys = idempotency_keys
                raise
            return DelegatedRunCancellationResult(updated_run, stored_turn, False)


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

    async def list_overdue(self, query: DelegatedRunOverdueQuery) -> list[AgentRun]:
        async with self.session_factory() as session:
            conditions = [
                AgentRunModel.delegated.is_(True),
                AgentRunModel.status.in_(["pending", "running", "blocked"]),
                AgentRunModel.deadline_at.is_not(None),
                AgentRunModel.deadline_at <= query.now,
            ]
            if query.tenant_id:
                conditions.append(AgentRunModel.tenant_id == query.tenant_id)
            rows = (
                (
                    await session.execute(
                        select(AgentRunModel)
                        .where(*conditions)
                        .order_by(AgentRunModel.deadline_at, AgentRunModel.run_id)
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
                run = await session.get(AgentRunModel, command.run_id)
                _validate_maintenance_event(existing_event, command, run)
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
        plan_repository: MemoryPlanRepository | None = None,
    ) -> None:
        self.run_repository = run_repository
        self.event_repository = event_repository
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        self.plan_repository = plan_repository
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

    async def list_overdue(self, query: DelegatedRunOverdueQuery) -> list[AgentRun]:
        runs = [
            run
            for run in self.run_repository.runs.values()
            if run.delegated
            and run.status in {"pending", "running", "blocked"}
            and run.deadline_at is not None
            and _as_utc(run.deadline_at) <= _as_utc(query.now)
            and (query.tenant_id is None or run.tenant_id == query.tenant_id)
        ]
        runs.sort(key=lambda run: (_as_utc(run.deadline_at), run.run_id))
        return [run.model_copy(deep=True) for run in runs[: query.limit]]

    async def timeout(self, command: DelegatedRunTimeoutCommand) -> tuple[AgentRun, bool]:
        async with self._lock:
            existing = await self.event_repository.get_event(
                command.event_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if existing is not None:
                run = await self.run_repository.get_run(command.run_id)
                _validate_maintenance_event(existing, command, run)
                return run, True
            run = _validate_timeout_run(await self.run_repository.get_run(command.run_id), command)
            turn = await self.turn_repository.get(
                command.turn_id,
                tenant_id=command.tenant_id,
                user_id=command.user_id,
            )
            if turn is None:
                raise DelegatedRunStartConflict("Timeout Turn ownership conflict")
            plan = None
            if command.plan_id:
                if self.plan_repository is None:
                    raise DelegatedRunStartConflict("Timeout Plan repository is not configured")
                plan = await self.plan_repository.get(
                    command.plan_id,
                    tenant_id=command.tenant_id,
                    user_id=command.user_id,
                )
                if plan is None:
                    raise DelegatedRunStartConflict("Timeout Plan not found")
            timed_out_at = max(_as_utc(command.occurred_at), _as_utc(turn.created_at))
            updated_run = run.model_copy(
                update={
                    "status": "timed_out",
                    "state_version": run.state_version + 1,
                    "event_sequence": run.event_sequence + 1,
                    "terminal_event_id": command.event_id,
                    "heartbeat_at": timed_out_at,
                    "updated_at": timed_out_at,
                }
            )
            updated_turn = turn.model_copy(
                update={
                    "status": TurnStatus.TIMED_OUT,
                    "state_version": turn.state_version + 1,
                    "updated_at": timed_out_at,
                    "completed_at": timed_out_at,
                }
            )
            updated_plan = (
                _timed_out_plan(plan, command, timed_out_at=timed_out_at)
                if plan is not None
                else None
            )
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
                created_at=timed_out_at,
            )
            outbox = TurnOutboxEvent(
                outbox_id=f"outbox_{uuid4().hex}",
                turn_id=command.turn_id,
                event_type="turn.timed_out",
                idempotency_key=f"turn.timed_out:{command.turn_id}",
                payload={"turn_id": command.turn_id, "run_id": command.run_id},
                available_at=timed_out_at,
            )
            snapshots = (
                run.model_copy(deep=True),
                turn.model_copy(deep=True),
                plan.model_copy(deep=True) if plan is not None else None,
                dict(self.event_repository.agent_events),
                dict(self.outbox_repository.events),
                dict(self.outbox_repository.idempotency_keys),
            )
            try:
                await self.run_repository.update_run(updated_run)
                if updated_plan is not None and self.plan_repository is not None:
                    await self.plan_repository.save(updated_plan)
                stored_turn = await self.turn_repository.update_if_version(
                    updated_turn,
                    expected_version=turn.state_version,
                )
                if stored_turn is None:
                    raise DelegatedRunStartConflict("Timeout Turn changed concurrently")
                await self.event_repository.add_agent_event(event)
                await self.outbox_repository.add_idempotent(outbox)
            except Exception:
                old_run, old_turn, old_plan, events, outbox_events, idempotency_keys = snapshots
                self.run_repository.runs[old_run.run_id] = old_run
                self.turn_repository.turns[old_turn.turn_id] = old_turn
                if old_plan is not None and self.plan_repository is not None:
                    self.plan_repository.plans[old_plan.plan_id] = old_plan
                self.event_repository.agent_events = events
                self.outbox_repository.events = outbox_events
                self.outbox_repository.idempotency_keys = idempotency_keys
                raise
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
    plan = await _lock_plan_row(
        session,
        plan_id=command.plan_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    step = await session.scalar(
        select(PlanStepModel)
        .where(
            PlanStepModel.plan_id == command.plan_id,
            PlanStepModel.step_id == command.step_id,
            PlanStepModel.agent_id == command.agent_id,
        )
        .with_for_update()
    )
    if (
        plan is None
        or step is None
        or plan.status not in {"pending", "running", "blocked"}
        or step.status not in {"pending", "running", "blocked"}
    ):
        raise DelegatedRunStartConflict("Plan Step ownership conflict")


async def _validate_memory_plan_step(
    plan_repository: MemoryPlanRepository | None,
    command: DelegatedRunStartCommand,
) -> None:
    if command.plan_id is None and command.step_id is None:
        return
    if not command.plan_id or not command.step_id:
        raise DelegatedRunStartConflict("Plan and Step must be supplied together")
    if plan_repository is None:
        raise DelegatedRunStartConflict("Plan repository is not configured")
    plan = await plan_repository.get(
        command.plan_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    step = (
        next(
            (
                item
                for item in plan.steps
                if item.step_id == command.step_id and item.agent_id == command.agent_id
            ),
            None,
        )
        if plan is not None
        else None
    )
    if (
        plan is None
        or step is None
        or plan.status not in {"pending", "running", "blocked"}
        or step.status not in {"pending", "running", "blocked"}
    ):
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
        agent_revision=command.agent_revision,
        handling_kind=command.handling_kind,
        binding_snapshot=command.binding_snapshot,
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


async def _project_database_clarification(
    session: AsyncSession,
    command: DelegatedRunProgressCommand,
) -> None:
    if command.event_type != "agent_clarify":
        return
    if not command.plan_id or not command.step_id:
        raise DelegatedRunStartConflict("Clarification requires Plan and Step")
    plan = await _lock_plan_row(
        session,
        plan_id=command.plan_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    step = await session.scalar(
        select(PlanStepModel)
        .where(
            PlanStepModel.plan_id == command.plan_id,
            PlanStepModel.step_id == command.step_id,
            PlanStepModel.agent_id == command.agent_id,
        )
        .with_for_update()
    )
    if (
        plan is None
        or step is None
        or plan.status not in {"pending", "running", "blocked"}
        or step.status not in {"pending", "running", "blocked"}
    ):
        raise DelegatedRunStartConflict("Clarification Plan Step association conflict")
    step.status = "blocked"
    plan.status = "blocked"
    plan.current_step_id = command.step_id
    plan.state_version += 1
    metadata = loads(plan.original_query, {})
    metadata.update(
        {
            "last_event_id": command.event_id,
            "state_version": plan.state_version,
            "formation_event_type": "update",
        }
    )
    plan.original_query = dumps(metadata)
    plan.updated_at = command.occurred_at


async def _memory_clarification_transition(
    plan_repository: MemoryPlanRepository | None,
    command: DelegatedRunProgressCommand,
) -> tuple[Plan | None, Plan | None]:
    if command.event_type != "agent_clarify":
        return None, None
    if not command.plan_id or not command.step_id:
        raise DelegatedRunStartConflict("Clarification requires Plan and Step")
    if plan_repository is None:
        raise DelegatedRunStartConflict("Clarification Plan repository is not configured")
    plan = await plan_repository.get(
        command.plan_id,
        tenant_id=command.tenant_id,
        user_id=command.user_id,
    )
    if plan is None or plan.status not in {"pending", "running", "blocked"}:
        raise DelegatedRunStartConflict("Clarification Plan Step association conflict")
    matched = False
    steps = []
    for step in plan.steps:
        if step.step_id == command.step_id and step.agent_id == command.agent_id:
            if step.status not in {"pending", "running", "blocked"}:
                raise DelegatedRunStartConflict("Clarification Plan Step association conflict")
            steps.append(step.model_copy(update={"status": "blocked"}))
            matched = True
        else:
            steps.append(step)
    if not matched:
        raise DelegatedRunStartConflict("Clarification Plan Step association conflict")
    updated = plan.model_copy(
        update={
            "status": "blocked",
            "current_step_id": command.step_id,
            "steps": steps,
            "state_version": plan.state_version + 1,
            "last_event_id": command.event_id,
            "updated_at": command.occurred_at,
            "formation_event_type": "update",
        }
    )
    return plan.model_copy(deep=True), updated


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
        "event_type": command.event_type,
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
        "event_type": event.event_type,
        "status": event.status,
        "sequence": event.sequence,
        "payload": (
            loads(event.payload_text, {}) if isinstance(event, AgentEventModel) else event.payload
        ),
    }
    expected = {
        "run_id": command.run_id,
        "turn_id": command.turn_id,
        "tenant_id": command.tenant_id,
        "user_id": command.user_id,
        "agent_id": command.agent_id,
        "plan_id": command.plan_id,
        "step_id": command.step_id,
        "event_type": command.event_type,
        "status": command.status,
        "sequence": command.sequence,
        "payload": command.payload,
    }
    if values != expected:
        raise DelegatedRunStartConflict("Event idempotency identity conflict")


def _validate_progress_event_namespace(command: DelegatedRunProgressCommand) -> None:
    if command.event_id.startswith(DELEGATED_RUN_MAINTENANCE_EVENT_PREFIX):
        raise DelegatedRunStartConflict("Event id is reserved for maintenance")


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


def _validate_failure_run(
    run: AgentRunModel | AgentRun | None,
    command: DelegatedRunFailCommand,
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
        raise DelegatedRunStartConflict("Delegated Run failure identity/state conflict")
    return canonical


def _validate_failure_turn(
    turn: CanonicalTurnModel | CanonicalTurn | None,
    command: DelegatedRunFailCommand,
) -> CanonicalTurn:
    if turn is None:
        raise DelegatedRunStartConflict("Canonical Turn not found")
    canonical = _turn_from_row(turn) if isinstance(turn, CanonicalTurnModel) else turn
    if (
        canonical.tenant_id != command.tenant_id
        or canonical.user_id != command.user_id
        or canonical.status.is_terminal
    ):
        raise DelegatedRunStartConflict("Canonical Turn failure ownership/state conflict")
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
    status, current_step_id = _plan_state_after_completion(steps)
    next_action = (
        plan.next_action
        if status == "blocked"
        and plan.next_action is not None
        and (plan.next_action.step_id is None or plan.next_action.step_id == current_step_id)
        else None
    )
    return plan.model_copy(
        update={
            "steps": steps,
            "status": status,
            "current_step_id": current_step_id,
            "next_action": next_action,
            "state_version": plan.state_version + 1,
            "last_event_id": command.event_id,
            "updated_at": command.occurred_at,
            "formation_event_type": "update",
        }
    )


def _plan_state_after_completion(steps) -> tuple[str, str | None]:
    if all(step.status == "completed" for step in steps):
        return "completed", None
    failed = next((step for step in steps if step.status == "failed"), None)
    if failed is not None:
        return "failed", None
    blocked = next((step for step in steps if step.status == "blocked"), None)
    if blocked is not None:
        return "blocked", blocked.step_id
    running = next((step for step in steps if step.status == "running"), None)
    if running is not None:
        return "running", running.step_id
    completed_ids = {step.step_id for step in steps if step.status == "completed"}
    ready = next(
        (
            step
            for step in steps
            if step.status == "pending"
            and all(dependency in completed_ids for dependency in step.depends_on)
        ),
        None,
    )
    if ready is not None:
        return "running", ready.step_id
    raise DelegatedRunStartConflict("Plan has incomplete Steps but no ready Step")


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


def _validate_completion_event_identity(
    event: AgentEventModel | AgentEvent,
    command: DelegatedRunCompleteCommand,
) -> None:
    _validate_final_event_identity(event, command)
    payload = loads(event.payload_text, {}) if isinstance(event, AgentEventModel) else event.payload
    if (
        event.event_type != "agent_result"
        or event.status != "completed"
        or payload != {"result_id": command.result_id}
    ):
        raise DelegatedRunStartConflict("Final Event idempotency identity conflict")


def _validate_completion_result_identity(
    result: AgentResult,
    command: DelegatedRunCompleteCommand,
) -> None:
    if (
        result.result_id != command.result_id
        or result.run_id != command.run_id
        or result.turn_id != command.turn_id
        or result.tenant_id != command.tenant_id
        or result.user_id != command.user_id
        or result.agent_id != command.agent_id
        or result.plan_id != command.plan_id
        or result.step_id != command.step_id
        or result.status != "completed"
        or result.message != command.response_text
        or result.output != command.output
        or result.artifact_refs != command.artifact_refs
    ):
        raise DelegatedRunStartConflict("Final Result idempotency identity conflict")


def _validate_failure_event_identity(
    event: AgentEventModel | AgentEvent,
    command: DelegatedRunFailCommand,
) -> None:
    _validate_final_event_identity(event, command)
    payload = loads(event.payload_text, {}) if isinstance(event, AgentEventModel) else event.payload
    if (
        event.event_type != "agent_error"
        or event.status != "failed"
        or payload != {"error": command.error}
    ):
        raise DelegatedRunStartConflict("Failure Event idempotency identity conflict")


def _validate_cancel_run(
    run: AgentRunModel | AgentRun | None,
    command: DelegatedRunCancelCommand,
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
        raise DelegatedRunStartConflict("Delegated Run cancel identity/state conflict")
    return canonical


def _validate_cancel_turn(
    turn: CanonicalTurnModel | CanonicalTurn | None,
    command: DelegatedRunCancelCommand,
) -> CanonicalTurn:
    if turn is None:
        raise DelegatedRunStartConflict("Canonical Turn not found")
    canonical = _turn_from_row(turn) if isinstance(turn, CanonicalTurnModel) else turn
    if (
        canonical.tenant_id != command.tenant_id
        or canonical.user_id != command.user_id
        or canonical.status.is_terminal
    ):
        raise DelegatedRunStartConflict("Canonical Turn cancel ownership/state conflict")
    return canonical


def _validate_cancel_event(
    event: AgentEventModel | AgentEvent,
    command: DelegatedRunCancelCommand,
) -> None:
    _validate_final_event_identity(event, command)
    payload = loads(event.payload_text, {}) if isinstance(event, AgentEventModel) else event.payload
    if (
        event.event_type != "agent_cancelled"
        or event.status != "cancelled"
        or payload != {"reason": command.reason}
    ):
        raise DelegatedRunStartConflict("Cancel Event idempotency identity conflict")


def _validate_cancelled_state(
    run: AgentRunModel | AgentRun | None,
    turn: CanonicalTurnModel | CanonicalTurn | None,
    command: DelegatedRunCancelCommand,
) -> tuple[AgentRun, CanonicalTurn]:
    if run is None or turn is None:
        raise DelegatedRunStartConflict("Cancellation has incomplete canonical state")
    canonical_run = _run_from_row(run) if isinstance(run, AgentRunModel) else run
    canonical_turn = _turn_from_row(turn) if isinstance(turn, CanonicalTurnModel) else turn
    if (
        canonical_run.status != "cancelled"
        or canonical_run.terminal_event_id != command.event_id
        or canonical_turn.status != TurnStatus.CANCELLED
    ):
        raise DelegatedRunStartConflict("Cancellation has conflicting canonical state")
    return canonical_run, canonical_turn


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
    command: DelegatedRunEventCommand,
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
    metadata = loads(plan.original_query, {})
    metadata.update(
        {
            "next_action": None,
            "last_event_id": command.event_id,
            "state_version": plan.state_version,
            "formation_event_type": "update",
        }
    )
    plan.original_query = dumps(metadata)
    plan.updated_at = command.occurred_at


async def _cancel_plan_step(
    session: AsyncSession,
    run: AgentRun,
    command: DelegatedRunCancelCommand,
    *,
    cancelled_at: datetime,
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
        raise DelegatedRunStartConflict("Cancel Plan Step association conflict")
    step.status = "cancelled"
    plan.status = "cancelled"
    plan.current_step_id = None
    plan.state_version += 1
    metadata = loads(plan.original_query, {})
    metadata.update(
        {
            "next_action": None,
            "last_event_id": command.event_id,
            "state_version": plan.state_version,
            "formation_event_type": "update",
        }
    )
    plan.original_query = dumps(metadata)
    plan.updated_at = cancelled_at


def _cancelled_plan(
    plan: Plan,
    command: DelegatedRunCancelCommand,
    *,
    cancelled_at: datetime,
) -> Plan:
    if not command.step_id:
        raise DelegatedRunStartConflict("Plan cancellation requires Step")
    matched = False
    steps = []
    for step in plan.steps:
        if step.step_id == command.step_id and step.agent_id == command.agent_id:
            steps.append(step.model_copy(update={"status": "cancelled"}))
            matched = True
        else:
            steps.append(step)
    if not matched:
        raise DelegatedRunStartConflict("Cancel Plan Step association conflict")
    return plan.model_copy(
        update={
            "steps": steps,
            "status": "cancelled",
            "current_step_id": None,
            "next_action": None,
            "state_version": plan.state_version + 1,
            "last_event_id": command.event_id,
            "updated_at": cancelled_at,
            "formation_event_type": "update",
        }
    )


def _timed_out_plan(
    plan: Plan,
    command: DelegatedRunTimeoutCommand,
    *,
    timed_out_at: datetime,
) -> Plan:
    if not command.step_id:
        raise DelegatedRunStartConflict("Plan timeout requires Step")
    matched = False
    steps = []
    for step in plan.steps:
        if step.step_id == command.step_id and step.agent_id == command.agent_id:
            steps.append(step.model_copy(update={"status": "failed"}))
            matched = True
        else:
            steps.append(step)
    if not matched:
        raise DelegatedRunStartConflict("Timeout Plan Step association conflict")
    return plan.model_copy(
        update={
            "steps": steps,
            "status": "failed",
            "current_step_id": None,
            "next_action": None,
            "state_version": plan.state_version + 1,
            "last_event_id": command.event_id,
            "updated_at": timed_out_at,
            "formation_event_type": "update",
        }
    )


def _failed_plan(
    plan: Plan,
    command: DelegatedRunFailCommand,
    failed_at: datetime,
) -> Plan:
    if not command.step_id:
        raise DelegatedRunStartConflict("Plan failure requires Step")
    matched = False
    steps = []
    for step in plan.steps:
        if step.step_id == command.step_id and step.agent_id == command.agent_id:
            steps.append(step.model_copy(update={"status": "failed"}))
            matched = True
        else:
            steps.append(step)
    if not matched:
        raise DelegatedRunStartConflict("Plan Step association conflict")
    return plan.model_copy(
        update={
            "steps": steps,
            "status": "failed",
            "current_step_id": None,
            "next_action": None,
            "state_version": plan.state_version + 1,
            "last_event_id": command.event_id,
            "updated_at": failed_at,
            "formation_event_type": "update",
        }
    )


def _failure_event_values(
    run: AgentRun,
    command: DelegatedRunFailCommand,
    failed_at: datetime,
) -> dict:
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
        "status": "failed",
        "plan_id": run.plan_id,
        "step_id": run.step_id,
        "sequence": run.event_sequence + 1,
        "run_state_version": run.state_version + 1,
        "payload_text": dumps({"error": command.error}),
        "created_at": failed_at,
    }


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


def _cancel_event_values(
    run: AgentRun,
    command: DelegatedRunCancelCommand,
    *,
    cancelled_at: datetime,
) -> dict:
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
        "event_type": "agent_cancelled",
        "status": "cancelled",
        "plan_id": run.plan_id,
        "step_id": run.step_id,
        "sequence": run.event_sequence + 1,
        "run_state_version": run.state_version + 1,
        "payload_text": dumps({"reason": command.reason}),
        "created_at": cancelled_at,
    }


def _validate_maintenance_event(
    event: AgentEventModel | AgentEvent,
    command: DelegatedRunTimeoutCommand,
    run: AgentRunModel | AgentRun | None,
) -> None:
    canonical_run = _run_from_row(run) if isinstance(run, AgentRunModel) else run
    payload = loads(event.payload_text, {}) if isinstance(event, AgentEventModel) else event.payload
    if (
        canonical_run is None
        or event.run_id != command.run_id
        or event.turn_id != command.turn_id
        or event.tenant_id != command.tenant_id
        or event.user_id != command.user_id
        or event.agent_id != command.agent_id
        or event.plan_id != command.plan_id
        or event.step_id != command.step_id
        or event.event_type != "agent_error"
        or event.status != "timed_out"
        or payload != {"reason": command.reason}
        or event.run_state_version != command.expected_state_version + 1
        or canonical_run.status != "timed_out"
        or canonical_run.terminal_event_id != command.event_id
        or canonical_run.state_version != event.run_state_version
        or canonical_run.event_sequence != event.sequence
        or canonical_run.deadline_at is None
        or _as_utc(canonical_run.deadline_at) != _as_utc(command.deadline_at)
    ):
        raise DelegatedRunStartConflict("Maintenance Event idempotency conflict")
