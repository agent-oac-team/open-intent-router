import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text

from app.core.config import Settings, get_settings
from app.db.models import AgentRunModel, PlanModel, PlanStepModel
from app.db.session import create_all_tables, create_session_factory
from app.dependencies import get_plan_executor, get_plan_service
from app.main import create_app
from app.repositories.database import (
    DatabasePlanRepository,
    DatabaseRunRepository,
    _run_values,
)
from app.repositories.delegated_runs import (
    DatabaseDelegatedRunStartStore,
    MemoryDelegatedRunStartStore,
)
from app.repositories.memory import MemoryPlanRepository, MemoryRunRepository
from app.repositories.turns import DatabaseTurnRepository, MemoryTurnRepository
from app.schemas.delegated_runs import DelegatedRunStartCommand
from app.schemas.logs import AgentRun
from app.schemas.plans import Plan, PlanStep
from app.schemas.turns import TurnUserInput
from app.services.delegated_run_service import DelegatedRunService
from app.services.plan_service import PlanService
from app.services.turn_service import TurnService
from tests.fakes.native_principal import native_principal_headers

_PRINCIPAL_SECRET = "plan-cancel-principal-secret"


class _UnusedExecutor:
    async def preflight(self, *_args, **_kwargs):
        raise AssertionError("cancel must not form a Candidate Set")


async def _client_for(plan: Plan, *, run: AgentRun | None = None):
    plans = MemoryPlanRepository()
    runs = MemoryRunRepository()
    await plans.save(plan)
    if run is not None:
        await runs.add_run(run)
    service = PlanService(plans, run_repository=runs)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        native_principal_secret=_PRINCIPAL_SECRET,
    )
    app.dependency_overrides[get_plan_service] = lambda: service
    app.dependency_overrides[get_plan_executor] = lambda: _UnusedExecutor()
    return TestClient(app), service


def _headers(*, subject: str = "user-1") -> dict[str, str]:
    return native_principal_headers(
        secret=_PRINCIPAL_SECRET,
        subject=subject,
        tenant="tenant-1",
    )


def _body(*, subject: str = "user-1") -> dict:
    return {
        "action": "cancel",
        "user": {"id": subject, "attributes": {"tenant_id": "tenant-1"}},
    }


async def test_active_delegated_run_returns_truthful_control_unsupported_repeatedly() -> None:
    plan = Plan(
        plan_id="plan-active",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        status="running",
        state_version=7,
        steps=[
            PlanStep(
                step_id="step-1",
                agent_id="agent-1",
                description="active",
                status="running",
            )
        ],
    )
    client, service = await _client_for(
        plan,
        run=AgentRun(
            run_id="run-active",
            session_id="session-1",
            agent_id="agent-1",
            tenant_id="tenant-1",
            user_id="user-1",
            plan_id="plan-active",
            step_id="step-1",
            status="running",
            invoker_type="delegated",
            delegated=True,
        ),
    )

    first = client.post(
        "/api/v1/plans/plan-active/actions",
        headers=_headers(),
        json=_body(),
    )
    repeated = client.post(
        "/api/v1/plans/plan-active/actions",
        headers=_headers(),
        json=_body(),
    )
    stored = await service.get_plan("plan-active", tenant_id="tenant-1", user_id="user-1")

    expected = {
        "accepted": False,
        "transitioned": False,
        "reason_code": "control_unsupported",
        "status": "running",
        "state_version": 7,
    }
    assert first.status_code == repeated.status_code == 200
    assert {key: first.json()[key] for key in expected} == expected
    assert {key: repeated.json()[key] for key in expected} == expected
    assert stored == plan


@pytest.mark.parametrize(
    ("status", "step_status"), [("pending", "pending"), ("blocked", "blocked")]
)
async def test_plan_without_active_execution_cancels_only_unstarted_work(
    status: str,
    step_status: str,
) -> None:
    plan = Plan(
        plan_id=f"plan-{status}",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        status=status,
        state_version=3,
        steps=[
            PlanStep(
                step_id="step-1",
                agent_id="agent-1",
                description="not active",
                status=step_status,
            )
        ],
    )
    client, service = await _client_for(plan)

    response = client.post(
        f"/api/v1/plans/{plan.plan_id}/actions",
        headers=_headers(),
        json=_body(),
    )
    stored = await service.get_plan(plan.plan_id, tenant_id="tenant-1", user_id="user-1")

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["transitioned"] is True
    assert response.json()["reason_code"] is None
    assert stored and stored.status == "cancelled" and stored.steps[0].status == "cancelled"
    assert stored.state_version == 4


async def test_plan_cancel_is_owner_isolated() -> None:
    plan = Plan(
        plan_id="plan-owned",
        tenant_id="tenant-1",
        user_id="user-1",
        status="pending",
        steps=[PlanStep(step_id="step-1", agent_id="agent-1", description="owned")],
    )
    client, service = await _client_for(plan)

    response = client.post(
        "/api/v1/plans/plan-owned/actions",
        headers=_headers(subject="other-user"),
        json=_body(subject="other-user"),
    )
    stored = await service.get_plan("plan-owned", tenant_id="tenant-1", user_id="user-1")

    assert response.status_code == 404
    assert stored == plan


@pytest.mark.parametrize("backend", ["memory", "database"])
@pytest.mark.parametrize("start_first", [False, True])
async def test_plan_cancel_and_delegated_start_have_only_serializable_outcomes(
    backend: str,
    start_first: bool,
    tmp_path,
) -> None:
    if backend == "memory":
        plans = MemoryPlanRepository()
        runs = MemoryRunRepository()
        turns_repository = MemoryTurnRepository()
        start_store = MemoryDelegatedRunStartStore(
            run_repository=runs,
            turn_repository=turns_repository,
            plan_repository=plans,
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'plan-cancel-start-race.db'}",
        )
        await create_all_tables(settings)
        factory = create_session_factory(settings)
        plans = DatabasePlanRepository(factory)
        runs = DatabaseRunRepository(factory)
        turns_repository = DatabaseTurnRepository(factory)
        start_store = DatabaseDelegatedRunStartStore(factory)

    await plans.save(
        Plan(
            plan_id="plan-race",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            status="running",
            state_version=3,
            steps=[
                PlanStep(
                    step_id="step-1",
                    agent_id="agent-1",
                    description="not started",
                    status="pending",
                )
            ],
        )
    )
    turn = await TurnService(turns_repository).start_turn(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-race",
        source="host",
        user_input=TurnUserInput(text="delegate"),
    )
    plan_service = PlanService(plans, run_repository=runs)
    run_service = DelegatedRunService(start_store)
    start_command = DelegatedRunStartCommand(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        request_id="request-race",
        turn_id=turn.turn.turn_id,
        agent_id="agent-1",
        plan_id="plan-race",
        step_id="step-1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    cancel_call = plan_service.cancel(
        "plan-race",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    start_call = run_service.start(start_command)
    if start_first:
        start, cancellation = await asyncio.gather(
            start_call,
            cancel_call,
            return_exceptions=True,
        )
    else:
        cancellation, start = await asyncio.gather(
            cancel_call,
            start_call,
            return_exceptions=True,
        )

    stored_plan = await plans.get("plan-race", tenant_id="tenant-1", user_id="user-1")
    active_run = await runs.get_active_delegated_run_for_plan(
        "plan-race",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    assert stored_plan is not None
    if stored_plan.status == "cancelled":
        assert active_run is None
        assert isinstance(start, Exception)
        assert not isinstance(cancellation, Exception)
        assert cancellation.transitioned is True
    else:
        assert active_run is not None
        assert not isinstance(start, Exception)
        assert not isinstance(cancellation, Exception)
        assert cancellation.accepted is False
        assert cancellation.reason_code == "control_unsupported"


async def test_real_postgresql_cancel_does_not_deadlock_with_run_first_terminal_path() -> None:
    async with _postgresql_lock_scope("lock-order") as scope:
        factory, engine, plans, runs, suffix, plan_id, run_id = scope
        run_lock_attempted = ThreadEvent()
        listener_registered = False
        terminal_session = factory()
        terminal_transaction = None
        cancellation_task = None

        def observe_cancel_run_lock(_conn, _cursor, statement, _parameters, _context, _many):
            if "FROM agent_runs" in statement and "FOR UPDATE" in statement:
                run_lock_attempted.set()

        try:
            await plans.save(
                Plan(
                    plan_id=plan_id,
                    tenant_id="tenant-lock-order",
                    user_id="user-lock-order",
                    session_id=f"session-{suffix}",
                    status="running",
                    state_version=1,
                    steps=[
                        PlanStep(
                            step_id="step-1",
                            agent_id="agent-1",
                            description="lock order",
                        )
                    ],
                )
            )
            await runs.add_run(
                AgentRun(
                    run_id=run_id,
                    session_id=f"session-{suffix}",
                    agent_id="agent-1",
                    tenant_id="tenant-lock-order",
                    user_id="user-lock-order",
                    plan_id=plan_id,
                    step_id="step-1",
                    status="running",
                    invoker_type="delegated",
                    delegated=True,
                )
            )
            terminal_transaction = await terminal_session.begin()
            await terminal_session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await terminal_session.execute(
                select(AgentRunModel).where(AgentRunModel.run_id == run_id).with_for_update()
            )
            event.listen(engine.sync_engine, "before_cursor_execute", observe_cancel_run_lock)
            listener_registered = True
            cancellation_task = asyncio.create_task(
                PlanService(plans, run_repository=runs).cancel(
                    plan_id,
                    tenant_id="tenant-lock-order",
                    user_id="user-lock-order",
                )
            )
            assert await asyncio.to_thread(run_lock_attempted.wait, 5)

            plan_lock_error = None
            try:
                await terminal_session.execute(
                    select(PlanModel).where(PlanModel.plan_id == plan_id).with_for_update()
                )
            except Exception as exc:
                plan_lock_error = exc
            if terminal_transaction.is_active:
                await terminal_transaction.rollback()
            cancellation = (await asyncio.gather(cancellation_task, return_exceptions=True))[0]

            assert plan_lock_error is None
            assert not isinstance(cancellation, BaseException)
            assert cancellation.reason_code == "control_unsupported"
        finally:
            if listener_registered:
                event.remove(engine.sync_engine, "before_cursor_execute", observe_cancel_run_lock)
            if terminal_transaction is not None and terminal_transaction.is_active:
                await terminal_transaction.rollback()
            if cancellation_task is not None:
                if not cancellation_task.done():
                    cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
            await terminal_session.close()


async def test_real_postgresql_cancel_observes_start_committed_while_waiting_for_plan() -> None:
    async with _postgresql_lock_scope("start-gap") as scope:
        factory, engine, plans, runs, suffix, plan_id, run_id = scope
        plan_lock_attempted = ThreadEvent()
        trigger_name = f"cancel_start_gap_{suffix}"
        function_name = f"sleep_cancel_start_gap_{suffix}"
        listener_registered = False
        start_session = factory()
        terminal_session = factory()
        start_transaction = None
        terminal_transaction = None
        cancellation_task = None

        def observe_cancel_plan_lock(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().startswith("UPDATE plans SET"):
                plan_lock_attempted.set()

        try:
            await plans.save(
                Plan(
                    plan_id=plan_id,
                    tenant_id="tenant-start-gap",
                    user_id="user-start-gap",
                    session_id=f"session-{suffix}",
                    status="running",
                    state_version=1,
                    steps=[
                        PlanStep(
                            step_id="step-1",
                            agent_id="agent-1",
                            description="start gap",
                        )
                    ],
                )
            )
            async with factory() as ddl, ddl.begin():
                await ddl.execute(
                    text(
                        f"""
                        CREATE FUNCTION {function_name}() RETURNS trigger
                        LANGUAGE plpgsql AS $$
                        BEGIN
                            PERFORM pg_sleep(2);
                            RETURN NEW;
                        END;
                        $$
                        """
                    )
                )
                await ddl.execute(
                    text(
                        f"""
                        CREATE TRIGGER {trigger_name}
                        AFTER UPDATE ON plans
                        FOR EACH ROW
                        WHEN (NEW.plan_id = '{plan_id}')
                        EXECUTE FUNCTION {function_name}()
                        """
                    )
                )

            start_transaction = await start_session.begin()
            await start_session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await start_session.execute(
                select(PlanModel).where(PlanModel.plan_id == plan_id).with_for_update()
            )
            event.listen(engine.sync_engine, "before_cursor_execute", observe_cancel_plan_lock)
            listener_registered = True
            cancellation_task = asyncio.create_task(
                PlanService(plans, run_repository=runs).cancel(
                    plan_id,
                    tenant_id="tenant-start-gap",
                    user_id="user-start-gap",
                )
            )
            assert await asyncio.to_thread(plan_lock_attempted.wait, 5)

            start_session.add(
                AgentRunModel(
                    **_run_values(
                        AgentRun(
                            run_id=run_id,
                            session_id=f"session-{suffix}",
                            agent_id="agent-1",
                            tenant_id="tenant-start-gap",
                            user_id="user-start-gap",
                            plan_id=plan_id,
                            step_id="step-1",
                            status="running",
                            invoker_type="delegated",
                            delegated=True,
                        )
                    )
                )
            )
            await start_session.flush()
            await start_transaction.commit()

            terminal_transaction = await terminal_session.begin()
            await terminal_session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await terminal_session.execute(
                select(AgentRunModel).where(AgentRunModel.run_id == run_id).with_for_update()
            )
            terminal_plan_error = None
            try:
                await terminal_session.execute(
                    select(PlanModel).where(PlanModel.plan_id == plan_id).with_for_update()
                )
            except Exception as exc:
                terminal_plan_error = exc
            if terminal_transaction.is_active:
                await terminal_transaction.rollback()
            cancellation = (await asyncio.gather(cancellation_task, return_exceptions=True))[0]

            assert terminal_plan_error is None
            assert not isinstance(cancellation, BaseException)
            assert cancellation.reason_code == "control_unsupported"
        finally:
            if listener_registered:
                event.remove(engine.sync_engine, "before_cursor_execute", observe_cancel_plan_lock)
            if start_transaction is not None and start_transaction.is_active:
                await start_transaction.rollback()
            if terminal_transaction is not None and terminal_transaction.is_active:
                await terminal_transaction.rollback()
            if cancellation_task is not None:
                if not cancellation_task.done():
                    cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
            await start_session.close()
            await terminal_session.close()
            async with factory() as ddl, ddl.begin():
                await ddl.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON plans"))
                await ddl.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))


@asynccontextmanager
async def _postgresql_lock_scope(label: str):
    settings = Settings(storage_backend="database", database_url=_postgresql_url())
    await create_all_tables(settings)
    factory = create_session_factory(settings)
    engine = factory.kw["bind"]
    suffix = uuid4().hex
    plan_id = f"plan-{label}-{suffix}"
    run_id = f"run-{label}-{suffix}"
    plans = DatabasePlanRepository(factory)
    runs = DatabaseRunRepository(factory)
    try:
        yield factory, engine, plans, runs, suffix, plan_id, run_id
    finally:
        try:
            async with factory() as cleanup, cleanup.begin():
                await cleanup.execute(delete(AgentRunModel).where(AgentRunModel.run_id == run_id))
                await cleanup.execute(delete(PlanStepModel).where(PlanStepModel.plan_id == plan_id))
                await cleanup.execute(delete(PlanModel).where(PlanModel.plan_id == plan_id))
        finally:
            await engine.dispose()


def _postgresql_url() -> str:
    value = os.getenv("OIR_TEST_POSTGRESQL_URL")
    if not isinstance(value, str) or not value.startswith(
        ("postgresql://", "postgresql+asyncpg://")
    ):
        pytest.skip("OIR_TEST_POSTGRESQL_URL is required for PostgreSQL locking semantics")
    return value
