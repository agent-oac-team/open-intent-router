from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.repositories.database import DatabaseRunRepository
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunMaintenanceStore,
    DelegatedRunStartConflict,
    MemoryDelegatedRunMaintenanceStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import MemoryEventRepository, MemoryRunRepository
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import MemoryTurnRepository
from app.runtime.application import ApplicationComposition
from app.schemas.delegated_runs import (
    DelegatedRunOrphanResponse,
    DelegatedRunOverdueQuery,
    DelegatedRunStartCommand,
)
from app.schemas.logs import AgentRun
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.delegated_run_timeout_runtime import (
    DelegatedRunTimeoutRuntime,
    timeout_event_id,
)
from app.services.turn_service import TurnService


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_overdue_query_uses_deadline_only_and_batches_deterministically(
    backend: str,
    tmp_path,
    managed_database,
) -> None:
    now = datetime.now(UTC)
    if backend == "memory":
        runs = MemoryRunRepository()
        store = MemoryDelegatedRunMaintenanceStore(
            run_repository=runs,
            event_repository=MemoryEventRepository(),
            turn_repository=MemoryTurnRepository(),
            outbox_repository=MemoryTurnOutboxRepository(),
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'overdue-query.db'}",
        )
        await managed_database.initialize_schema(settings)
        factory = await managed_database.session_factory(settings)
        runs = DatabaseRunRepository(factory)
        store = DatabaseDelegatedRunMaintenanceStore(factory)
    values = [
        ("due-first", "pending", now - timedelta(seconds=2), True),
        ("due-second", "blocked", now - timedelta(seconds=1), True),
        ("future-stale", "running", now + timedelta(minutes=5), True),
        ("terminal", "completed", now - timedelta(minutes=1), True),
        ("not-delegated", "pending", now - timedelta(minutes=1), False),
    ]
    for run_id, status, deadline, delegated in values:
        await runs.add_run(
            AgentRun(
                run_id=run_id,
                session_id="session-1",
                agent_id="agent-1",
                tenant_id="tenant-1",
                user_id="user-1",
                turn_id=f"turn-{run_id}",
                status=status,
                invoker_type="delegated" if delegated else "mock",
                delegated=delegated,
                deadline_at=deadline,
                heartbeat_at=now - timedelta(days=1),
            )
        )

    first_batch = await store.list_overdue(DelegatedRunOverdueQuery(now=now, limit=1))
    all_due = await store.list_overdue(DelegatedRunOverdueQuery(now=now, limit=10))

    assert [run.run_id for run in first_batch] == ["due-first"]
    assert [run.run_id for run in all_due] == ["due-first", "due-second"]


async def test_timeout_runtime_is_deterministic_and_restart_idempotent() -> None:
    now = datetime.now(UTC)
    runs = MemoryRunRepository()
    events = MemoryEventRepository()
    turns = MemoryTurnRepository()
    outbox = MemoryTurnOutboxRepository()
    maintenance = MemoryDelegatedRunMaintenanceStore(
        run_repository=runs,
        event_repository=events,
        turn_repository=turns,
        outbox_repository=outbox,
    )
    service = DelegatedRunService(
        MemoryDelegatedRunStartStore(run_repository=runs, turn_repository=turns),
        maintenance_store=maintenance,
    )
    turn = await TurnService(turns).start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-1",
        source="host",
        user_input=TurnUserInput(text="deadline"),
    )
    started = await service.start(
        DelegatedRunStartCommand(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            request_id="request-1",
            turn_id=turn.turn.turn_id,
            agent_id="agent-1",
            deadline_at=now - timedelta(seconds=1),
        )
    )
    first_runtime = DelegatedRunTimeoutRuntime(
        service,
        interval_seconds=1,
        batch_size=10,
        clock=lambda: now,
    )

    assert await first_runtime.run_once() == 1
    restarted = DelegatedRunTimeoutRuntime(
        service,
        interval_seconds=1,
        batch_size=10,
        clock=lambda: now,
    )
    assert await restarted.run_once() == 0

    expected_event_id = timeout_event_id(started.run)
    event = await events.get_event(
        expected_event_id,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert event and event.status == "timed_out"
    assert len(events.agent_events) == 1
    assert len(outbox.events) == 1


async def test_timeout_runtime_skips_terminal_race_and_stops_cleanly() -> None:
    now = datetime.now(UTC)
    reference = _reference(now)

    class RacingService:
        async def list_overdue(self, _query):
            return DelegatedRunOrphanResponse(runs=[reference])

        async def timeout(self, _command):
            raise DelegatedRunStartConflict("terminal transition won")

    runtime = DelegatedRunTimeoutRuntime(
        RacingService(),
        interval_seconds=0.01,
        batch_size=1,
        clock=lambda: now,
    )

    assert await runtime.run_once() == 0
    await runtime.start()
    assert runtime.running is True
    await runtime.stop()
    assert runtime.running is False


def test_fastapi_lifespan_starts_and_awaits_timeout_runtime() -> None:
    from dataclasses import replace

    from app import main as main_module

    calls: list[str] = []

    class LifecycleProbe:
        async def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    settings = Settings(storage_backend="memory", registry_backend="file")

    def container_builder(catalog, databases):
        container = main_module._build_minimal_container(
            settings=settings,
            catalog=catalog,
            databases=databases,
            external_executor=None,
        )
        return replace(container, _background_runtimes=(LifecycleProbe(),))

    with TestClient(
        main_module.create_app(
            settings=settings,
            application_composition_factory=lambda _settings: ApplicationComposition(
                container_builder=container_builder,
                required_database_targets=frozenset(),
            ),
        )
    ) as client:
        assert client.get("/health").status_code == 200
        assert calls == ["start"]

    assert calls == ["start", "stop"]


def _reference(now: datetime):
    from app.schemas.delegated_runs import DelegatedRunReference, DelegatedRunStatus

    return DelegatedRunReference(
        run_id="run-race",
        turn_id="turn-race",
        tenant_id="tenant-1",
        user_id="user-1",
        agent_id="agent-1",
        status=DelegatedRunStatus.RUNNING,
        state_version=2,
        deadline_at=now - timedelta(seconds=1),
    )
