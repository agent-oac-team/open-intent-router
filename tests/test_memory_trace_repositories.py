from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.models import MemoryFormationJobModel
from app.db.session import create_all_tables, create_session_factory
from app.repositories.context_stores import DatabaseMemoryItemRepository
from app.repositories.memory_formation import (
    DatabaseMemoryFormationTurnJobRepository,
    _job_values,
)
from app.repositories.memory_traces import (
    DatabaseMemoryFormationTraceRepository,
    MemoryFormationTraceRepository,
)
from app.schemas.memory import (
    MemoryDecisionStatus,
    MemoryEvent,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTrace,
    MemoryFormationTrigger,
    MemoryFormationTurn,
    MemoryLifecycleOperation,
    MemoryOperation,
)


async def test_in_memory_trace_repository_does_not_expose_mutable_state() -> None:
    repository = MemoryFormationTraceRepository()
    trace = MemoryFormationTrace(
        job=MemoryFormationJob(
            trigger="structured_event",
            mode="observe",
            tenant_id="t1",
            user_id="u1",
            idempotency_key="structured:event_1:v1",
            model_version="projector-v1",
            prompt_version="projector-v1",
            policy_version="policy-v1",
        ),
        request_ids=["request_1"],
        usage={"nested": {"tokens": 1}},
    )
    returned = await repository.add_trace(trace)
    trace.job.tenant_id = "attacker-tenant"
    returned.request_ids.clear()
    returned.usage["nested"]["tokens"] = 99
    first_read = await repository.list_traces(tenant_id="t1", request_id="request_1")
    assert len(first_read) == 1
    assert first_read[0].usage == {"nested": {"tokens": 1}}
    first_read[0].job.user_id = "attacker"
    second_read = await repository.list_traces(tenant_id="t1", user_id="u1")
    assert len(second_read) == 1


async def test_database_trace_filters_compose_on_durable_columns(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-traces.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
    await formation.append_turn(
        MemoryFormationTurn(
            turn_id="turn_1",
            request_id="request_1",
            session_id="session_1",
            run_id="run_1",
            user_id="u1",
            tenant_id="t1",
            agent_id="agent_1",
            result_status="completed",
            completed_at=datetime(2026, 7, 13, tzinfo=UTC),
        )
    )
    job = await formation.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="session_1",
        trigger=MemoryFormationTrigger.IDLE,
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    events = DatabaseMemoryItemRepository(session_factory)
    await events.add_event(
        MemoryEvent(
            event_type="memory_decision_pending",
            tenant_id="t1",
            user_id="u1",
            agent_id="agent_1",
            request_id="request_1",
            session_id="session_1",
            turn_id="turn_1",
            run_id="run_1",
            formation_job_id=job.job_id,
            memory_key="tenant:t1:user:u1:preference:language",
            decision_status="pending",
            scope="user_preference",
            payload={
                "operation": MemoryLifecycleOperation(
                    operation_id="operation_pending",
                    operation=MemoryOperation.PENDING,
                    decision_status=MemoryDecisionStatus.PENDING,
                    reason_code=MemoryFormationReasonCode.CONFIDENCE_PENDING,
                    tenant_id="t1",
                    user_id="u1",
                    subject_type="user",
                    subject_id="u1",
                    memory_key="tenant:t1:user:u1:preference:language",
                    candidate_hash="sha256:pending",
                    formation_job_id=job.job_id,
                ).model_dump(mode="json")
            },
        )
    )
    traces = DatabaseMemoryFormationTraceRepository(session_factory)
    result = await traces.list_traces(
        tenant_id="t1",
        user_id="u1",
        agent_id="agent_1",
        scope="user_preference",
        request_id="request_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
        formation_job_id=job.job_id,
        memory_key="tenant:t1:user:u1:preference:language",
        decision_status="pending",
    )
    assert len(result) == 1
    assert result[0].turn_ids == ["turn_1"]
    assert result[0].agent_ids == ["agent_1"]
    assert result[0].scopes == ["user_preference"]
    assert await traces.list_traces(tenant_id="t2", formation_job_id=job.job_id) == []


async def test_database_trace_filters_structured_event_job_without_turns(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'structured-traces.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    job = MemoryFormationJob(
        trigger="structured_event",
        mode="observe",
        tenant_id="t1",
        user_id="u1",
        idempotency_key="structured:event_1:v1",
        model_version="projector-v1",
        prompt_version="projector-v1",
        policy_version="policy-v1",
    )
    async with session_factory() as session:
        session.add(MemoryFormationJobModel(**_job_values(job)))
        await session.commit()
    events = DatabaseMemoryItemRepository(session_factory)
    await events.add_event(
        MemoryEvent(
            event_type="formation_decision",
            tenant_id="t1",
            user_id="u1",
            agent_id="agent_1",
            request_id="request_1",
            session_id="session_1",
            run_id="run_1",
            formation_job_id=job.job_id,
            memory_key="tenant:t1:user:u1:plan:p1:task_status",
            decision_status="pending",
            scope="task_memory",
        )
    )
    traces = DatabaseMemoryFormationTraceRepository(session_factory)
    filters = {
        "request_id": "request_1",
        "session_id": "session_1",
        "run_id": "run_1",
        "agent_id": "agent_1",
        "memory_key": "tenant:t1:user:u1:plan:p1:task_status",
    }
    for name, value in filters.items():
        assert len(await traces.list_traces(tenant_id="t1", **{name: value})) == 1
    result = await traces.list_traces(tenant_id="t1", **filters)
    assert len(result) == 1
    assert result[0].request_ids == ["request_1"]
    assert result[0].run_ids == ["run_1"]
    assert result[0].agent_ids == ["agent_1"]


async def test_database_effective_pending_filter_is_applied_before_limit(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'effective-pending-limit.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
    events = DatabaseMemoryItemRepository(session_factory)
    now = datetime(2026, 7, 13, tzinfo=UTC)
    active_job_id = None
    for index in range(4):
        resolved = index > 0
        job = await formation.add_job(
            MemoryFormationJob(
                job_id=f"job_effective_{index}",
                trigger="structured_event",
                mode="observe",
                tenant_id="t1",
                user_id="u1",
                idempotency_key=f"effective-{index}",
                model_version="model-v1",
                prompt_version="prompt-v1",
                policy_version="policy-v1",
                created_at=now + timedelta(seconds=index),
                updated_at=now + timedelta(seconds=index),
            )
        )
        decision_id = f"decision_effective_{index}"
        operation = MemoryLifecycleOperation(
            operation_id=f"operation_effective_{index}",
            operation=MemoryOperation.PENDING,
            decision_status=MemoryDecisionStatus.PENDING,
            reason_code=MemoryFormationReasonCode.CONFIDENCE_PENDING,
            tenant_id="t1",
            user_id="u1",
            subject_type="user",
            subject_id="u1",
            memory_key=f"tenant:t1:user:u1:fact:{index}",
            candidate_hash=f"sha256:effective-{index}",
            formation_job_id=job.job_id,
        )
        await events.add_event(
            MemoryEvent(
                event_id=decision_id,
                event_type="memory_decision_pending",
                tenant_id="t1",
                user_id="u1",
                formation_job_id=job.job_id,
                decision_status="pending",
                scope="stable_fact",
                payload={"operation": operation.model_dump(mode="json")},
                created_at=now + timedelta(seconds=index),
            )
        )
        if resolved:
            await events.add_event(
                MemoryEvent(
                    event_id=f"completion_effective_{index}",
                    event_type="memory_pending_confirm",
                    tenant_id="t1",
                    user_id="u1",
                    formation_job_id=job.job_id,
                    decision_status="resolved",
                    decision_id=decision_id,
                    scope="stable_fact",
                    payload={"decision_id": decision_id, "action": "confirm"},
                    created_at=now + timedelta(seconds=index, microseconds=1),
                )
            )
        else:
            active_job_id = job.job_id

    pending = await DatabaseMemoryFormationTraceRepository(session_factory).list_traces(
        tenant_id="t1", user_id="u1", decision_status="pending", limit=1
    )
    assert [trace.job.job_id for trace in pending] == [active_job_id]
