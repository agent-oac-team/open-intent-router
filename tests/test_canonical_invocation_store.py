from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.core.config import Settings
from app.core.errors import InvocationDeadlineExceededError
from app.db.models import AgentResultModel, AgentRunModel, CanonicalTurnModel, TurnOutboxModel
from app.repositories.canonical_invocations import (
    DatabaseCanonicalInvocationStore,
    MemoryCanonicalInvocationStore,
)
from app.repositories.database import DatabaseResultRepository, DatabaseRunRepository
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.turns import FormationEligibilitySnapshot, TurnUserInput
from app.services.turn_service import TurnService


class _CommitAcknowledgementLossFactory:
    """Raise after one committed session scope to model an ACK loss."""

    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.exits = 0
        self.raise_on_exit: int | None = None

    def __call__(self):
        return _CommitAcknowledgementLossSession(self.delegate(), self)

    def lose_next_transaction_acknowledgement(self) -> None:
        # ``complete_run`` first reads the Turn in one scope, then its
        # coordinator commits in the next scope.
        self.raise_on_exit = self.exits + 2


class _CommitAcknowledgementLossSession:
    def __init__(self, delegate: object, owner: _CommitAcknowledgementLossFactory) -> None:
        self.delegate = delegate
        self.owner = owner

    async def __aenter__(self):
        await self.delegate.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> object:
        result = await self.delegate.__aexit__(*args)
        self.owner.exits += 1
        if self.owner.raise_on_exit == self.owner.exits:
            self.owner.raise_on_exit = None
            raise RuntimeError("simulated canonical completion acknowledgement loss")
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


@pytest.fixture(params=["memory", "database"])
async def canonical_store(request, tmp_path, managed_database):
    if request.param == "memory":
        runs = MemoryRunRepository()
        results = MemoryResultRepository()
        turns = MemoryTurnRepository()
        outbox = MemoryTurnOutboxRepository()
        return (
            MemoryCanonicalInvocationStore(
                run_repository=runs,
                result_repository=results,
                turn_repository=turns,
                outbox_repository=outbox,
            ),
            TurnService(turns),
            runs,
            results,
            turns,
            outbox,
        )
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'canonical-invocation.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    return (
        DatabaseCanonicalInvocationStore(factory),
        TurnService(DatabaseTurnRepository(factory)),
        DatabaseRunRepository(factory),
        DatabaseResultRepository(factory),
        DatabaseTurnRepository(factory),
        factory,
    )


async def test_canonical_invocation_store_closes_turn_and_persists_outbox(
    canonical_store,
) -> None:
    store, turns, _, _, _, backend = canonical_store
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host_chat",
        user_input=TurnUserInput(text="prepare visit; I like pork"),
    )
    now = datetime.now(UTC)
    run, active_turn, replay, created = await store.start_run(
        AgentRun(
            run_id="run-1",
            request_id="request-1",
            session_id="session-1",
            agent_id="visit-preparation",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
            created_at=now,
            updated_at=now,
        )
    )
    assert replay is None
    assert created is True
    assert run.turn_id == started.turn.turn_id
    assert active_turn.status.value == "running"

    completed_run, result, completed_turn = await store.complete_run(
        run=run.model_copy(update={"status": "completed", "output": {"ok": True}}),
        result=AgentResult(
            result_id="result-1",
            run_id=run.run_id,
            session_id=run.session_id,
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            status="completed",
            message="visit prepared",
            output={"ok": True},
            created_at=now,
        ),
        response_text="visit prepared",
        eligibility=FormationEligibilitySnapshot(
            mode="enforced", policy_version="formation-policy-v1"
        ),
    )
    assert completed_run.status == "completed"
    assert result.turn_id == completed_turn.turn_id
    assert completed_turn.status.value == "completed"
    assert completed_turn.references.run_ids == ["run-1"]
    assert completed_turn.references.result_ids == ["result-1"]

    if isinstance(backend, MemoryTurnOutboxRepository):
        assert len(backend.events) == 1
        event = next(iter(backend.events.values()))
        assert event.payload["formation_eligibility"]["mode"] == "enforced"
    else:
        async with backend() as session:
            stored_run = await session.get(AgentRunModel, "run-1")
            stored_result = await session.get(AgentResultModel, "result-1")
            stored_turn = await session.get(CanonicalTurnModel, completed_turn.turn_id)
            outbox = (await session.execute(select(TurnOutboxModel))).scalar_one()
        assert stored_run and stored_run.status == "completed"
        assert stored_result and stored_result.turn_id == completed_turn.turn_id
        assert stored_turn and stored_turn.status == "completed"
        assert outbox.payload_text.find('"mode":"enforced"') >= 0


async def test_canonical_invocation_store_persists_confirmed_stop_as_cancelled(
    canonical_store,
) -> None:
    """A Runtime-confirmed stop is a canonical cancellation, not a failure."""

    store, turns, _, _, _, backend = canonical_store
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-cancel",
        request_id="request-cancel",
        source="host_chat",
        user_input=TurnUserInput(text="cancel the work"),
    )
    now = datetime.now(UTC)
    run, _, replay, created = await store.start_run(
        AgentRun(
            run_id="run-cancelled",
            request_id="request-cancel",
            session_id="session-cancel",
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="runtime-adapter",
            created_at=now,
            updated_at=now,
        )
    )
    assert replay is None and created is True

    completed_run, result, completed_turn = await store.complete_run(
        run=run.model_copy(update={"status": "cancelled"}),
        result=AgentResult(
            result_id="result-cancelled",
            run_id=run.run_id,
            session_id=run.session_id,
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            status="cancelled",
            message="Agent invocation was cancelled.",
            created_at=now,
        ),
        response_text="Agent invocation was cancelled.",
        eligibility=FormationEligibilitySnapshot(
            mode="enforced", policy_version="formation-policy-v1"
        ),
    )

    assert completed_run.status == result.status == "cancelled"
    assert completed_turn.status.value == "cancelled"
    assert completed_turn.turn_id == started.turn.turn_id
    if isinstance(backend, MemoryTurnOutboxRepository):
        event = next(iter(backend.events.values()))
        assert event.event_type == "turn.cancelled"
    else:
        async with backend() as session:
            event = (await session.execute(select(TurnOutboxModel))).scalar_one()
        assert event.event_type == "turn.cancelled"


async def test_canonical_invocation_store_replays_terminal_result(canonical_store) -> None:
    store, turns, _, _, _, _ = canonical_store
    await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-replay",
        source="host_chat",
        user_input=TurnUserInput(text="same request"),
    )
    now = datetime.now(UTC)
    run, _, _, _ = await store.start_run(
        AgentRun(
            run_id="run-first",
            request_id="request-replay",
            session_id="session-1",
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
            created_at=now,
            updated_at=now,
        )
    )
    await store.complete_run(
        run=run.model_copy(update={"status": "completed"}),
        result=AgentResult(
            result_id="result-first",
            run_id=run.run_id,
            session_id=run.session_id,
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            status="completed",
            message="done",
            created_at=now,
        ),
        response_text="done",
        eligibility=FormationEligibilitySnapshot(
            mode="enforced", policy_version="formation-policy-v1"
        ),
    )

    replay_run, replay_turn, replay_result, created = await store.start_run(
        run.model_copy(update={"run_id": "run-second", "turn_id": None})
    )
    assert replay_run.run_id == "run-first"
    assert replay_turn.status.value == "completed"
    assert replay_result and replay_result.result_id == "result-first"
    assert created is False


async def test_stable_run_conflict_from_another_turn_never_mutates_the_loser_turn(
    canonical_store,
) -> None:
    """A Plan-start retry can share a Run id while its request Turn differs."""

    store, turns, _, _, _, _ = canonical_store
    first = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-stable-run-a",
        source="host_chat",
        user_input=TurnUserInput(text="first request"),
    )
    second = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-stable-run-b",
        source="host_chat",
        user_input=TurnUserInput(text="second request"),
    )
    now = datetime.now(UTC)
    first_run, _, _, created = await store.start_run(
        AgentRun(
            run_id="run_stable-plan-execution",
            request_id=first.turn.request_id,
            session_id=first.turn.session_id,
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
            created_at=now,
            updated_at=now,
        )
    )
    assert created is True

    replay_run, losing_turn, replay_result, created = await store.start_run(
        first_run.model_copy(
            update={
                "request_id": second.turn.request_id,
                "session_id": second.turn.session_id,
                "turn_id": None,
            }
        )
    )

    assert created is False
    assert replay_result is None
    assert replay_run.run_id == first_run.run_id
    assert losing_turn.turn_id == second.turn.turn_id
    current_second = await turns.get_turn(
        turn_id=second.turn.turn_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert current_second is not None
    assert current_second.status.value == "pending"
    assert current_second.references.run_ids == []


async def test_database_start_guard_rechecks_at_the_real_transaction_commit(
    tmp_path,
    managed_database,
) -> None:
    """The context-exit commit cannot outrun a pre-acceptance deadline guard."""

    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'canonical-start-guard.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    store = DatabaseCanonicalInvocationStore(factory)
    turns = TurnService(DatabaseTurnRepository(factory))
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-start-guard",
        source="host_chat",
        user_input=TurnUserInput(text="guard commit"),
    )
    checks = 0

    def expires_at_commit() -> bool:
        nonlocal checks
        checks += 1
        # start_run checks before mutation, after flush, and SQLAlchemy calls
        # the attached guard one final time from its actual before_commit hook.
        return checks < 3

    with pytest.raises(InvocationDeadlineExceededError):
        await store.start_run(
            AgentRun(
                run_id="run-start-guard",
                request_id="request-start-guard",
                session_id="session-1",
                agent_id="agent-1",
                user_id="user-1",
                tenant_id="tenant-1",
                status="running",
                invoker_type="mock",
            ),
            may_commit=expires_at_commit,
        )

    assert checks == 3
    async with factory() as session:
        assert await session.get(AgentRunModel, "run-start-guard") is None
        turn = await session.get(CanonicalTurnModel, started.turn.turn_id)
    assert turn is not None
    assert turn.status == "pending"


async def test_database_direct_start_guard_rechecks_at_connection_commit(
    tmp_path,
    managed_database,
) -> None:
    """A deadline that flips after Session hooks still prevents DBAPI commit."""

    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'direct-start-connection-guard.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    engine = factory.kw["bind"].sync_engine
    expired = False

    def expire_immediately_before_dbapi_commit(_connection) -> None:
        nonlocal expired
        expired = True

    event.listen(engine, "commit", expire_immediately_before_dbapi_commit)
    try:
        with pytest.raises(InvocationDeadlineExceededError):
            await DatabaseRunRepository(factory).add_run(
                AgentRun(
                    run_id="run-direct-start-connection-guard",
                    request_id="request-direct-start-connection-guard",
                    session_id="session-1",
                    agent_id="agent-1",
                    user_id="user-1",
                    tenant_id="tenant-1",
                    status="running",
                    invoker_type="mock",
                ),
                may_commit=lambda: not expired,
            )
    finally:
        event.remove(engine, "commit", expire_immediately_before_dbapi_commit)

    assert expired is True
    async with factory() as session:
        assert await session.get(AgentRunModel, "run-direct-start-connection-guard") is None


async def test_database_canonical_completion_replays_after_commit_acknowledgement_loss(
    tmp_path,
    managed_database,
) -> None:
    """A committed canonical terminal bundle is replayed instead of surfaced as a 500."""

    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'canonical-completion-ack-loss.db'}",
    )
    await managed_database.initialize_schema(settings)
    factory = await managed_database.session_factory(settings)
    tracked_factory = _CommitAcknowledgementLossFactory(factory)
    store = DatabaseCanonicalInvocationStore(tracked_factory)  # type: ignore[arg-type]
    turns = TurnService(DatabaseTurnRepository(factory))
    started = await turns.start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="canonical-completion-ack-request",
        source="host_chat",
        user_input=TurnUserInput(text="complete once"),
    )
    now = datetime.now(UTC)
    run, _, _, created = await store.start_run(
        AgentRun(
            run_id="run-canonical-completion-ack",
            request_id=started.turn.request_id,
            session_id=started.turn.session_id,
            agent_id="agent-1",
            user_id="user-1",
            tenant_id="tenant-1",
            status="running",
            invoker_type="mock",
            created_at=now,
            updated_at=now,
        )
    )
    assert created is True

    tracked_factory.lose_next_transaction_acknowledgement()
    completed_run, result, completed_turn = await store.complete_run(
        run=run.model_copy(update={"status": "completed", "output": {"ok": True}}),
        result=AgentResult(
            result_id="result-canonical-completion-ack",
            run_id=run.run_id,
            session_id=run.session_id,
            agent_id=run.agent_id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            status="completed",
            message="done",
            output={"ok": True},
            created_at=now,
        ),
        response_text="done",
        eligibility=FormationEligibilitySnapshot(
            mode="enforced", policy_version="formation-policy-v1"
        ),
    )

    assert completed_run.status == "completed"
    assert result.result_id == "result-canonical-completion-ack"
    assert completed_turn.status.value == "completed"
    async with factory() as session:
        stored_run = await session.get(AgentRunModel, run.run_id)
        stored_result = await session.get(AgentResultModel, result.result_id)
        stored_turn = await session.get(CanonicalTurnModel, completed_turn.turn_id)
    assert stored_run is not None and stored_run.status == "completed"
    assert stored_result is not None and stored_result.status == "completed"
    assert stored_turn is not None and stored_turn.status == "completed"
