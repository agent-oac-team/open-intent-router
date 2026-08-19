from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.database import (
    DatabaseEventRepository,
    DatabaseResultRepository,
    DatabaseRunRepository,
)
from app.schemas.events import AgentEvent
from app.schemas.logs import (
    AgentResult,
    AgentRun,
    ExternalExecutionBindingSnapshot,
    external_execution_binding_fingerprint,
)


async def test_delegated_run_result_event_fields_round_trip_database(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'delegated-fields.db'}",
    )
    await create_all_tables(settings)
    factory = create_session_factory(settings)
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="run-1",
        request_id="request-1",
        session_id="session-1",
        agent_id="agent-1",
        user_id="user-1",
        tenant_id="tenant-1",
        turn_id="turn-1",
        plan_id="plan-1",
        step_id="step-1",
        status="running",
        invoker_type="delegated",
        agent_revision=7,
        handling_kind="external_execution",
        binding_snapshot=ExternalExecutionBindingSnapshot(
            executor_ref="host_executor",
            executor_binding_id=external_execution_binding_fingerprint("external_binding"),
        ),
        delegated=True,
        state_version=2,
        deadline_at=now + timedelta(minutes=5),
        heartbeat_at=now,
        claim_owner="host-1",
        claim_token="claim-token",
        claim_expires_at=now + timedelta(seconds=30),
    )
    stored_run = await DatabaseRunRepository(factory).add_run(run)
    result = AgentResult(
        result_id="result-1",
        run_id=run.run_id,
        session_id=run.session_id,
        agent_id=run.agent_id,
        user_id=run.user_id,
        tenant_id=run.tenant_id,
        turn_id=run.turn_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
        status="completed",
        run_state_version=3,
    )
    stored_result = await DatabaseResultRepository(factory).add_result(result)
    event = AgentEvent(
        event_id="event-1",
        run_id=run.run_id,
        request_id=run.request_id,
        session_id=run.session_id,
        agent_id=run.agent_id,
        user_id=run.user_id,
        tenant_id=run.tenant_id,
        turn_id=run.turn_id,
        event_type="agent_progress",
        status="running",
        plan_id=run.plan_id,
        step_id=run.step_id,
        sequence=1,
        run_state_version=2,
    )
    stored_event, duplicate = await DatabaseEventRepository(factory).add_agent_event(event)

    assert stored_run.turn_id == "turn-1" and stored_run.delegated is True
    assert stored_run.state_version == 2 and stored_run.claim_token == "claim-token"
    assert stored_run.agent_revision == 7
    assert stored_run.handling_kind == "external_execution"
    assert stored_run.binding_snapshot == ExternalExecutionBindingSnapshot(
        executor_ref="host_executor",
        executor_binding_id=external_execution_binding_fingerprint("external_binding"),
    )
    assert stored_result.turn_id == "turn-1" and stored_result.run_state_version == 3
    assert duplicate is False
    assert stored_event.turn_id == "turn-1" and stored_event.sequence == 1
    assert stored_event.run_state_version == 2
