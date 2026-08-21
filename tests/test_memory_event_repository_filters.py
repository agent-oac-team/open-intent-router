from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.repositories.context_stores import DatabaseMemoryItemRepository, MemoryItemRepository
from app.schemas.memory import MemoryEvent


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_memory_event_filters_are_composed_inside_repository(
    backend, tmp_path, managed_database
) -> None:
    if backend == "memory":
        repository = MemoryItemRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'memory-events.db'}",
        )
        await managed_database.initialize_schema(settings)
        repository = DatabaseMemoryItemRepository(await managed_database.session_factory(settings))
    target = MemoryEvent(
        event_type="memory_decision_pending",
        memory_id="mem_1",
        user_id="u1",
        tenant_id="t1",
        agent_id="agent_1",
        request_id="request_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
        formation_job_id="job_1",
        memory_key="tenant:t1:user:u1:preference:language",
        decision_status="pending",
    )
    await repository.add_event(target)
    await repository.add_event(
        target.model_copy(
            update={
                "event_id": "other-tenant",
                "tenant_id": "t2",
                "request_id": "request_1",
            }
        )
    )
    await repository.add_event(
        target.model_copy(update={"event_id": "other-job", "formation_job_id": "job_2"})
    )
    await repository.add_event(target.model_copy(update={"event_id": "unowned", "user_id": None}))

    events = await repository.list_events(
        tenant_id="t1",
        user_id="u1",
        request_id="request_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
        formation_job_id="job_1",
        memory_key="tenant:t1:user:u1:preference:language",
        decision_status="pending",
    )
    assert [event.event_id for event in events] == [target.event_id]


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_effective_pending_event_filter_is_applied_before_limit(
    backend, tmp_path, managed_database
) -> None:
    if backend == "memory":
        repository = MemoryItemRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'effective-pending-events.db'}",
        )
        await managed_database.initialize_schema(settings)
        repository = DatabaseMemoryItemRepository(await managed_database.session_factory(settings))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    active_id = "decision_0"
    for index in range(4):
        decision_id = f"decision_{index}"
        await repository.add_event(
            MemoryEvent(
                event_id=decision_id,
                event_type="memory_decision_pending",
                tenant_id="t1",
                user_id="u1",
                decision_status="pending",
                created_at=now + timedelta(seconds=index),
            )
        )
        if index:
            await repository.add_event(
                MemoryEvent(
                    event_id=f"resolution_{index}",
                    event_type="memory_pending_confirm",
                    tenant_id="t1",
                    user_id="u1",
                    decision_status="resolved",
                    decision_id=decision_id,
                    payload={"decision_id": decision_id, "action": "confirm"},
                    created_at=now + timedelta(seconds=index, microseconds=1),
                )
            )

    pending = await repository.list_events(
        tenant_id="t1", user_id="u1", decision_status="pending", limit=1
    )
    assert [event.event_id for event in pending] == [active_id]
