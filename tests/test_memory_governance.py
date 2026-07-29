from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory_index_operations import MemoryIndexOutboxRepository
from app.schemas.memory import MemoryIndexOperation, MemoryItem
from app.services.memory_governance import MemoryGovernanceService


def item(memory_id: str, age: int, *, content: str = "safe test content") -> MemoryItem:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    return MemoryItem(
        memory_id=memory_id,
        scope="user_preference",
        subject_id="user-1",
        user_id="user-1",
        tenant_id="tenant-1",
        content=content,
        lifecycle_status="deletion_pending",
        index_status="deletion_pending",
        updated_at=now - timedelta(seconds=age),
    )


@pytest.mark.asyncio
async def test_governance_applies_300_second_boundary_and_excludes_normal_index_state() -> None:
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    await repository.add(item("mem-299", 299))
    await repository.add(item("mem-300", 300))
    await repository.add(item("other-tenant", 300).model_copy(update={"tenant_id": "tenant-2"}))
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )

    response = await service.query(tenant_id="tenant-1")

    assert [entry.memory_id for entry in response.items] == ["mem-300"]
    assert response.items[0].anomaly == "deletion_timeout"


@pytest.mark.asyncio
async def test_governance_immediately_includes_dead_letter_residual_and_canonical_gap() -> None:
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    for memory_id in ("dead", "residual", "unclosed"):
        await repository.add(item(memory_id, 1, content="" if memory_id == "dead" else "body"))
    await outbox.add(
        MemoryIndexOperation(
            idempotency_key="dead",
            operation="delete",
            memory_id="dead",
            tenant_id="tenant-1",
            status="dead_letter",
        )
    )
    await outbox.add(
        MemoryIndexOperation(
            idempotency_key="residual",
            operation="delete",
            memory_id="residual",
            tenant_id="tenant-1",
            status="dead_letter",
            external_memory_id="provider-1",
        )
    )
    await outbox.add(
        MemoryIndexOperation(
            idempotency_key="unclosed",
            operation="delete",
            memory_id="unclosed",
            tenant_id="tenant-1",
            status="completed",
        )
    )
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )

    response = await service.query(tenant_id="tenant-1")

    assert {entry.anomaly for entry in response.items} == {
        "deletion_dead_letter",
        "provider_residual",
        "canonical_not_closed",
    }
    assert (
        next(entry for entry in response.items if entry.memory_id == "dead").content_state
        == "cleared"
    )


@pytest.mark.asyncio
async def test_governance_repair_requeues_dead_letter_and_replays_without_duplicate_side_effect() -> (
    None
):
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    await repository.add(item("dead", 1))
    operation = await outbox.add(
        MemoryIndexOperation(
            idempotency_key="dead",
            operation="delete",
            memory_id="dead",
            tenant_id="tenant-1",
            status="dead_letter",
            attempt_count=5,
            max_attempts=5,
        )
    )
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
            lifecycle_store=SimpleNamespace(),
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    target = (await service.query(tenant_id="tenant-1")).items[0]

    accepted = await service.repair(
        tenant_id="tenant-1",
        memory_id="dead",
        expected_version=target.version,
        expected_anomaly=target.anomaly,
        idempotency_key="repair-1",
    )
    replay = await service.repair(
        tenant_id="tenant-1",
        memory_id="dead",
        expected_version=target.version,
        expected_anomaly=target.anomaly,
        idempotency_key="repair-1",
    )

    stored = await outbox.get(operation.index_operation_id, tenant_id="tenant-1")
    assert accepted.accepted is True
    assert replay.idempotent_replay is True
    assert stored is not None
    assert stored.status == "pending"
    assert stored.attempt_count == 0
    assert (await service.query(tenant_id="tenant-1")).items[0].status == "repairing"


@pytest.mark.asyncio
async def test_governance_repair_rejects_stale_version_and_unrepairable_state_without_side_effect() -> (
    None
):
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    await repository.add(item("dead", 1))
    operation = await outbox.add(
        MemoryIndexOperation(
            idempotency_key="dead",
            operation="delete",
            memory_id="dead",
            tenant_id="tenant-1",
            status="dead_letter",
        )
    )
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
            lifecycle_store=SimpleNamespace(),
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )

    rejected = await service.repair(
        tenant_id="tenant-1",
        memory_id="dead",
        expected_version="stale",
        expected_anomaly="deletion_dead_letter",
        idempotency_key="repair-1",
    )
    missing = await service.repair(
        tenant_id="tenant-1",
        memory_id="missing",
        expected_version="none",
        expected_anomaly="deletion_timeout",
        idempotency_key="repair-2",
    )

    stored = await outbox.get(operation.index_operation_id, tenant_id="tenant-1")
    assert rejected.accepted is False
    assert missing.accepted is False
    assert stored is not None and stored.status == "dead_letter"


@pytest.mark.asyncio
async def test_governance_repair_closes_canonical_after_provider_completion() -> None:
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    await repository.add(item("unclosed", 1))
    await outbox.add(
        MemoryIndexOperation(
            idempotency_key="unclosed",
            operation="delete",
            memory_id="unclosed",
            tenant_id="tenant-1",
            status="completed",
        )
    )

    class LifecycleStore:
        calls = 0

        async def complete_delete_from_index(self, operation, **_kwargs):
            self.calls += 1
            repository.items.pop(operation.memory_id, None)

    lifecycle = LifecycleStore()
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
            lifecycle_store=lifecycle,
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    target = (await service.query(tenant_id="tenant-1")).items[0]

    accepted = await service.repair(
        tenant_id="tenant-1",
        memory_id="unclosed",
        expected_version=target.version,
        expected_anomaly=target.anomaly,
        idempotency_key="repair-close",
    )
    replay = await service.repair(
        tenant_id="tenant-1",
        memory_id="unclosed",
        expected_version=target.version,
        expected_anomaly=target.anomaly,
        idempotency_key="repair-close",
    )

    assert accepted.action == "canonical_close"
    assert replay.idempotent_replay is True
    assert lifecycle.calls == 1
    assert (await service.query(tenant_id="tenant-1")).items == []
