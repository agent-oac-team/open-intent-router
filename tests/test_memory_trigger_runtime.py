import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.main import create_app
from app.repositories.memory_formation import (
    DatabaseMemoryFormationTurnJobRepository,
    MemoryFormationTurnJobRepository,
)
from app.schemas.memory import MemoryFormationJobStatus, MemoryFormationTurn
from app.services.memory_formation import (
    FormationIdleSweeper,
    FormationJobWorker,
    FormationTriggerCoordinator,
    MemoryFormationRuntime,
    structured_event_idempotency_key,
)
from app.services.memory_maintenance import MemoryMaintenanceRuntime

_TEST_ENGINES = []


@pytest.fixture(autouse=True)
async def _dispose_test_engines():
    yield
    while _TEST_ENGINES:
        await _TEST_ENGINES.pop().dispose()


def _settings(**updates) -> Settings:
    values = {
        "storage_backend": "memory",
        "memory_mode": "observe",
        "memory_formation_window_turns": 5,
        "memory_formation_idle_seconds": 30,
        "memory_formation_model_timeout_seconds": 0.1,
        "memory_formation_lease_seconds": 5,
        "memory_formation_max_attempts": 2,
        "memory_formation_retry_base_seconds": 1,
        "memory_formation_retry_max_seconds": 4,
        "memory_formation_sweep_interval_seconds": 0.01,
    }
    return Settings(**{**values, **updates})


def _turn(index: int, *, completed_at: datetime | None = None) -> MemoryFormationTurn:
    completed = completed_at or datetime(2026, 7, 13, 0, 0, index, tzinfo=UTC)
    return MemoryFormationTurn(
        turn_id=f"turn_{index}",
        request_id=f"request_{index}",
        session_id="session_1",
        run_id=f"run_{index}",
        user_id="u1",
        tenant_id="t1",
        agent_id="agent_1",
        user_text=f"user-{index}",
        assistant_text=f"assistant-{index}",
        result_status="completed",
        completed_at=completed,
    )


async def _repository(backend: str, tmp_path):
    if backend == "memory":
        return MemoryFormationTurnJobRepository()
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / f'trigger-{backend}.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    _TEST_ENGINES.append(session_factory.kw["bind"])
    return DatabaseMemoryFormationTurnJobRepository(session_factory)


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_coordinator_freezes_five_turns_resets_idle_and_starts_next_range(
    backend, tmp_path
) -> None:
    repository = await _repository(backend, tmp_path)
    settings = _settings(storage_backend=backend)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=repository)
    for index in range(1, 5):
        _, job = await coordinator.append_and_check(_turn(index))
        assert job is None
    pending = await repository.list_pending_turns(
        tenant_id="t1", user_id="u1", session_id="session_1"
    )
    expected_deadline = _turn(4).completed_at + timedelta(seconds=30)
    assert {turn.idle_deadline_at for turn in pending} == {expected_deadline}

    _, job = await coordinator.append_and_check(_turn(5))
    assert job
    assert job.source_refs == [f"turn_{index}" for index in range(1, 6)]
    assert job.first_turn_id == "turn_1" and job.last_turn_id == "turn_5"
    _, next_job = await coordinator.append_and_check(_turn(6))
    assert next_job is None
    next_pending = await repository.list_pending_turns(
        tenant_id="t1", user_id="u1", session_id="session_1"
    )
    assert [turn.turn_id for turn in next_pending] == ["turn_6"]


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_idle_sweeper_recovers_persisted_deadline_after_restart(backend, tmp_path) -> None:
    repository = await _repository(backend, tmp_path)
    settings = _settings(storage_backend=backend)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=repository)
    for index in range(1, 4):
        await coordinator.append_and_check(_turn(index))
    deadline = _turn(3).completed_at + timedelta(seconds=30)

    restarted_sweeper = FormationIdleSweeper(settings=settings, repository=repository)
    assert await restarted_sweeper.run_once(now=deadline - timedelta(microseconds=1)) == []
    jobs = await restarted_sweeper.run_once(now=deadline)
    assert len(jobs) == 1
    assert jobs[0].source_refs == ["turn_1", "turn_2", "turn_3"]
    assert await restarted_sweeper.run_once(now=deadline) == []


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_late_out_of_order_turn_does_not_move_idle_deadline_backward(
    backend, tmp_path
) -> None:
    repository = await _repository(backend, tmp_path)
    settings = _settings(storage_backend=backend)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=repository)
    later = datetime(2026, 7, 13, 1, tzinfo=UTC)
    await coordinator.append_and_check(_turn(1, completed_at=later))
    await coordinator.append_and_check(_turn(2, completed_at=later - timedelta(seconds=20)))
    pending = await repository.list_pending_turns(
        tenant_id="t1", user_id="u1", session_id="session_1"
    )
    assert {turn.idle_deadline_at for turn in pending} == {later + timedelta(seconds=30)}


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_fifth_turn_and_idle_race_create_only_one_executable_job(backend, tmp_path) -> None:
    repository = await _repository(backend, tmp_path)
    settings = _settings(storage_backend=backend)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=repository)
    for index in range(1, 5):
        await coordinator.append_and_check(_turn(index))
    old_deadline = _turn(4).completed_at + timedelta(seconds=30)
    sweeper = FormationIdleSweeper(settings=settings, repository=repository)

    await asyncio.gather(
        coordinator.append_and_check(
            _turn(5, completed_at=_turn(4).completed_at + timedelta(seconds=1))
        ),
        sweeper.run_once(now=old_deadline),
    )

    now = old_deadline + timedelta(seconds=1)
    assert await repository.claim_job(owner="worker-a", now=now, lease_seconds=5)
    assert await repository.claim_job(owner="worker-b", now=now, lease_seconds=5) is None


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_new_turn_during_claimed_job_starts_next_range(backend, tmp_path) -> None:
    repository = await _repository(backend, tmp_path)
    settings = _settings(storage_backend=backend)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=repository)
    job = None
    for index in range(1, 6):
        _, job = await coordinator.append_and_check(_turn(index))
    assert job
    now = _turn(5).completed_at + timedelta(seconds=1)
    claimed = await repository.claim_job(owner="worker", now=now, lease_seconds=5)
    assert claimed
    await coordinator.append_and_check(_turn(6))
    pending = await repository.list_pending_turns(
        tenant_id="t1", user_id="u1", session_id="session_1"
    )
    assert [turn.turn_id for turn in pending] == ["turn_6"]
    await repository.complete_job(
        claimed.job_id,
        owner="worker",
        lease_token=claimed.lease_token,
        now=now + timedelta(seconds=1),
    )
    assert (
        await repository.successful_watermark(tenant_id="t1", user_id="u1", session_id="session_1")
        == "turn_5"
    )


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_duplicate_turn_capture_is_idempotent(backend, tmp_path) -> None:
    repository = await _repository(backend, tmp_path)
    settings = _settings(storage_backend=backend)
    coordinator = FormationTriggerCoordinator(settings=settings, repository=repository)
    first, second = await asyncio.gather(
        coordinator.append_and_check(_turn(1)),
        coordinator.append_and_check(_turn(1)),
    )

    assert first[0].turn_id == second[0].turn_id == "turn_1"
    pending = await repository.list_pending_turns(
        tenant_id="t1", user_id="u1", session_id="session_1"
    )
    assert [turn.turn_id for turn in pending] == ["turn_1"]


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_same_request_id_is_isolated_by_tenant_user_and_session(backend, tmp_path) -> None:
    repository = await _repository(backend, tmp_path)
    first = _turn(1).model_copy(update={"request_id": "shared-request"})
    second = _turn(2).model_copy(
        update={
            "request_id": "shared-request",
            "tenant_id": "t2",
            "user_id": "u2",
            "session_id": "session_2",
        }
    )
    await asyncio.gather(repository.append_turn(first), repository.append_turn(second))

    assert (
        len(
            await repository.list_pending_turns(
                tenant_id="t1", user_id="u1", session_id="session_1"
            )
        )
        == 1
    )
    assert (
        len(
            await repository.list_pending_turns(
                tenant_id="t2", user_id="u2", session_id="session_2"
            )
        )
        == 1
    )


async def test_two_database_repository_instances_share_sqlite_session_coordination(
    tmp_path,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'shared-coordination.db'}",
    )
    await create_all_tables(settings)
    first_factory = create_session_factory(settings)
    second_factory = create_session_factory(settings)
    _TEST_ENGINES.extend([first_factory.kw["bind"], second_factory.kw["bind"]])
    first_repository = DatabaseMemoryFormationTurnJobRepository(first_factory)
    second_repository = DatabaseMemoryFormationTurnJobRepository(second_factory)
    runtime_settings = _settings(storage_backend="database")
    coordinator = FormationTriggerCoordinator(
        settings=runtime_settings, repository=first_repository
    )
    for index in range(1, 5):
        await coordinator.append_and_check(_turn(index))
    old_deadline = _turn(4).completed_at + timedelta(seconds=30)

    await asyncio.gather(
        coordinator.append_and_check(
            _turn(5, completed_at=_turn(4).completed_at + timedelta(seconds=1))
        ),
        FormationIdleSweeper(settings=runtime_settings, repository=second_repository).run_once(
            now=old_deadline
        ),
    )

    now = old_deadline + timedelta(seconds=1)
    assert await first_repository.claim_job(owner="worker-a", now=now, lease_seconds=5)
    assert await second_repository.claim_job(owner="worker-b", now=now, lease_seconds=5) is None


class RecordingProcessor:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[bool] = []

    async def process(self, job, *, execute_lifecycle: bool) -> dict:
        del job
        self.calls.append(execute_lifecycle)
        if len(self.calls) <= self.failures:
            raise RuntimeError("provider secret must not be persisted")
        return {"decision_counts": {"add": 1}}


class HangingFailureProjectionProcessor(RecordingProcessor):
    def __init__(self) -> None:
        super().__init__(failures=1)
        self.projection_started = False

    async def project_failure(self, _job, *, reason_code: str) -> None:
        assert reason_code == "formation_processor_error"
        self.projection_started = True
        await asyncio.Event().wait()


async def _idle_job(repository, settings):
    coordinator = FormationTriggerCoordinator(settings=settings, repository=repository)
    await coordinator.append_and_check(_turn(1))
    deadline = _turn(1).completed_at + timedelta(seconds=30)
    jobs = await FormationIdleSweeper(settings=settings, repository=repository).run_once(
        now=deadline
    )
    assert len(jobs) == 1
    return deadline, jobs[0]


@pytest.mark.parametrize(("mode", "execute_lifecycle"), [("observe", False), ("on", True)])
async def test_worker_rollout_mode_controls_lifecycle_execution(mode, execute_lifecycle) -> None:
    repository = MemoryFormationTurnJobRepository()
    settings = _settings(memory_mode=mode)
    now, _ = await _idle_job(repository, settings)
    processor = RecordingProcessor()
    worker = FormationJobWorker(
        settings=settings,
        repository=repository,
        processor=processor,
        owner="worker",
        clock=lambda: now,
    )

    completed = await worker.run_once()

    assert completed and completed.status == MemoryFormationJobStatus.COMPLETED
    assert processor.calls == [execute_lifecycle]
    assert completed.trace_summary["decision_counts"] == {"add": 1}
    assert completed.trace_summary["job_latency_ms"] >= 0
    assert completed.trace_summary["model_latency_ms"] >= 0


async def test_worker_retries_then_dead_letters_without_advancing_watermark() -> None:
    repository = MemoryFormationTurnJobRepository()
    settings = _settings(memory_formation_max_attempts=2)
    current, _ = await _idle_job(repository, settings)
    processor = RecordingProcessor(failures=2)
    clock_value = [current]
    worker = FormationJobWorker(
        settings=settings,
        repository=repository,
        processor=processor,
        owner="worker",
        clock=lambda: clock_value[0],
    )

    first = await worker.run_once()
    assert first and first.status == MemoryFormationJobStatus.RETRY
    assert first.last_error_code == "formation_processor_error"
    clock_value[0] = first.next_attempt_at
    second = await worker.run_once()

    assert second and second.status == MemoryFormationJobStatus.DEAD_LETTER
    assert (
        await repository.successful_watermark(tenant_id="t1", user_id="u1", session_id="session_1")
        is None
    )
    assert "provider secret" not in str(second.model_dump())


async def test_hanging_failure_projection_cannot_block_retry_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.memory_formation._TRACE_PROJECTION_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    repository = MemoryFormationTurnJobRepository()
    settings = _settings()
    current, _ = await _idle_job(repository, settings)
    processor = HangingFailureProjectionProcessor()
    worker = FormationJobWorker(
        settings=settings,
        repository=repository,
        processor=processor,
        owner="worker",
        clock=lambda: current,
    )

    failed = await asyncio.wait_for(worker.run_once(), timeout=0.1)

    assert failed and failed.status == MemoryFormationJobStatus.RETRY
    assert failed.last_error_code == "formation_processor_error"
    assert processor.projection_started is True


async def test_worker_retry_replay_completes_once_and_advances_watermark() -> None:
    repository = MemoryFormationTurnJobRepository()
    settings = _settings(memory_formation_max_attempts=3)
    current, _ = await _idle_job(repository, settings)
    processor = RecordingProcessor(failures=1)
    clock_value = [current]
    worker = FormationJobWorker(
        settings=settings,
        repository=repository,
        processor=processor,
        owner="worker",
        clock=lambda: clock_value[0],
    )

    retry = await worker.run_once()
    assert retry and retry.status == MemoryFormationJobStatus.RETRY
    clock_value[0] = retry.next_attempt_at
    completed = await worker.run_once()

    assert completed and completed.status == MemoryFormationJobStatus.COMPLETED
    assert len(processor.calls) == 2
    assert (
        await repository.successful_watermark(tenant_id="t1", user_id="u1", session_id="session_1")
        == "turn_1"
    )


class BlockingProcessor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def process(self, job, *, execute_lifecycle: bool) -> dict:
        del job, execute_lifecycle
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return {}


async def test_duplicate_workers_have_one_processor_winner() -> None:
    repository = MemoryFormationTurnJobRepository()
    settings = _settings()
    now, _ = await _idle_job(repository, settings)
    processor = BlockingProcessor()
    first_worker = FormationJobWorker(
        settings=settings,
        repository=repository,
        processor=processor,
        owner="worker-a",
        clock=lambda: now,
    )
    second_worker = FormationJobWorker(
        settings=settings,
        repository=repository,
        processor=processor,
        owner="worker-b",
        clock=lambda: now,
    )
    first_task = asyncio.create_task(first_worker.run_once())
    await processor.started.wait()

    assert await second_worker.run_once() is None
    processor.release.set()
    completed = await first_task
    assert completed and completed.status == MemoryFormationJobStatus.COMPLETED
    assert processor.calls == 1


async def test_runtime_starts_and_stops_worker_and_sweeper_gracefully() -> None:
    repository = MemoryFormationTurnJobRepository()
    settings = _settings(memory_mode="observe")
    worker = FormationJobWorker(
        settings=settings,
        repository=repository,
        processor=RecordingProcessor(),
        owner="worker",
    )
    runtime = MemoryFormationRuntime(
        settings=settings,
        worker=worker,
        sweeper=FormationIdleSweeper(settings=settings, repository=repository),
    )

    await runtime.start()
    assert len(runtime._tasks) == 2
    assert runtime.status.state == "running"
    assert runtime.status.worker_running is True
    assert runtime.status.sweeper_running is True
    await asyncio.sleep(0)
    await runtime.stop()
    assert runtime._tasks == []
    assert runtime.status.state == "stopped"
    assert runtime.status.worker_running is False


async def test_runtime_loops_recover_from_transient_repository_errors() -> None:
    settings = _settings(memory_mode="observe")

    class FlakyWorker:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient worker error")
            return None

    class FlakySweeper:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient sweeper error")
            return []

    worker = FlakyWorker()
    sweeper = FlakySweeper()
    runtime = MemoryFormationRuntime(settings=settings, worker=worker, sweeper=sweeper)
    await runtime.start()
    await asyncio.sleep(0.03)
    await runtime.stop()

    assert worker.calls >= 2
    assert sweeper.calls >= 2
    assert runtime.status.last_error_code in {
        "formation_worker_error",
        "formation_sweeper_error",
    }


async def test_maintenance_runtime_consumes_index_and_ttl_work_when_formation_is_off() -> None:
    settings = _settings(
        memory_mode="off",
        memory_maintenance_interval_seconds=0.01,
    )

    class Processor:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            return None if self.calls > 1 else ["processed"]

    class Service:
        index_worker = Processor()
        ttl_sweeper = Processor()

    service = Service()
    runtime = MemoryMaintenanceRuntime(settings=settings, memory_service=service)
    await runtime.start()
    await asyncio.sleep(0.03)
    assert runtime.status.state == "running"
    assert runtime.status.index_worker_running is True
    assert runtime.status.ttl_sweeper_running is True
    assert service.index_worker.calls >= 2
    assert service.ttl_sweeper.calls >= 2
    await runtime.stop()
    assert runtime._tasks == []
    assert runtime.status.state == "stopped"


async def test_reconciler_runs_when_idle_sweeper_is_disabled() -> None:
    settings = _settings(memory_mode="off")

    class Reconciler:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            return {}

    reconciler = Reconciler()
    runtime = MemoryFormationRuntime(
        settings=settings,
        worker=object(),
        sweeper=object(),
        reconciler=reconciler,
    )
    await runtime.start()
    assert len(runtime._tasks) == 1
    await asyncio.sleep(0.03)
    await runtime.stop()
    assert reconciler.calls >= 2


async def test_application_lifespan_starts_and_stops_formation_runtime() -> None:
    from dataclasses import replace

    calls = []

    class FakeRuntime:
        async def start(self) -> None:
            calls.append("start")

        async def stop(self) -> None:
            calls.append("stop")

    from app import main as main_module

    settings = Settings(storage_backend="memory", registry_backend="file")

    def container_builder(catalog, databases):
        container = main_module._build_minimal_container(
            settings=settings,
            catalog=catalog,
            databases=databases,
            external_executor=None,
        )
        return replace(container, _background_runtimes=(FakeRuntime(), FakeRuntime()))

    app = create_app(settings=settings, application_container_builder=container_builder)

    async with app.router.lifespan_context(app):
        assert calls == ["start", "start"]
    assert calls == ["start", "start", "stop", "stop"]


def test_structured_event_idempotency_key_is_stable_and_version_scoped() -> None:
    values = {
        "tenant_id": "t1",
        "user_id": "u1",
        "source_type": "plan",
        "source_id": "plan_1",
        "source_version": "v1",
    }
    assert structured_event_idempotency_key(**values) == structured_event_idempotency_key(**values)
    assert structured_event_idempotency_key(**values) != structured_event_idempotency_key(
        **{**values, "source_version": "v2"}
    )
    assert structured_event_idempotency_key(**values) != structured_event_idempotency_key(
        **{**values, "tenant_id": "t2"}
    )
