from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory_index_operations import MemoryIndexOutboxRepository
from app.schemas.memory import MemoryGovernanceResponse, MemoryIndexOperation, MemoryItem
from app.services.memory_governance import MemoryGovernanceService
from host_adapters.oac.api.memory_governance import router
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.identity.models import TrustedHostIdentity
from host_apps.oac.dependencies import (
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)


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
    assert response.items[0].repairable is False
    assert response.items[0].status == "blocked"
    assert response.items[0].safe_reason == "缺少删除清理操作，无法自动修复"


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
async def test_governance_status_filter_applies_before_pagination_and_keeps_global_health() -> None:
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    for memory_id in ("attention-1", "attention-2", "blocked"):
        await repository.add(item(memory_id, 301))
    for memory_id in ("attention-1", "attention-2"):
        await outbox.add(
            MemoryIndexOperation(
                idempotency_key=memory_id,
                operation="delete",
                memory_id=memory_id,
                tenant_id="tenant-1",
                status="pending",
            )
        )
    await outbox.add(
        MemoryIndexOperation(
            idempotency_key="blocked",
            operation="delete",
            memory_id="blocked",
            tenant_id="tenant-1",
            status="dead_letter",
        )
    )
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )

    response = await service.query(
        tenant_id="tenant-1",
        status="blocked",
        page=1,
        page_size=1,
    )
    empty_filter = await service.query(
        tenant_id="tenant-1",
        status="repairing",
        page=1,
        page_size=1,
    )

    assert [entry.memory_id for entry in response.items] == ["blocked"]
    assert response.total == 1
    assert response.healthy is False
    assert empty_filter.items == []
    assert empty_filter.total == 0
    assert empty_filter.healthy is False


@pytest.mark.asyncio
async def test_governance_pagination_counts_anomalies_beyond_the_first_scan_batch() -> None:
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    for index in range(1001):
        memory_id = f"mem-{index:04}"
        await repository.add(item(memory_id, 301))
        await outbox.add(
            MemoryIndexOperation(
                idempotency_key=memory_id,
                operation="delete",
                memory_id=memory_id,
                tenant_id="tenant-1",
                status="pending",
            )
        )
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )

    response = await service.query(
        tenant_id="tenant-1",
        page=51,
        page_size=20,
    )

    assert response.total == 1001
    assert len(response.items) == 1


@pytest.mark.asyncio
async def test_governance_batches_index_lookups_for_large_queues() -> None:
    repository = MemoryItemRepository()

    class CountingOutbox(MemoryIndexOutboxRepository):
        batch_calls = 0
        single_calls = 0

        async def list_for_memory(self, *args, **kwargs):
            self.single_calls += 1
            return await super().list_for_memory(*args, **kwargs)

        async def latest_deletes_for_memories(self, *args, **kwargs):
            self.batch_calls += 1
            return await super().latest_deletes_for_memories(*args, **kwargs)

    outbox = CountingOutbox()
    for index in range(1001):
        memory_id = f"mem-{index:04}"
        await repository.add(item(memory_id, 301))
        await outbox.add(
            MemoryIndexOperation(
                idempotency_key=memory_id,
                operation="delete",
                memory_id=memory_id,
                tenant_id="tenant-1",
                status="pending",
            )
        )
    service = MemoryGovernanceService(
        memory_service=SimpleNamespace(
            repository=repository,
            index_worker=SimpleNamespace(outbox=outbox),
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )

    response = await service.query(tenant_id="tenant-1", page=51, page_size=20)

    assert response.total == 1001
    assert outbox.batch_calls == 2
    assert outbox.single_calls == 0


def test_oac_governance_query_forwards_status_filter_to_authoritative_service(
    non_lifespan_test_client,
) -> None:
    class GovernancePort:
        seen_status = None

        async def query(self, **kwargs):
            self.seen_status = kwargs.get("status")
            return MemoryGovernanceResponse(healthy=False)

    governance = GovernancePort()
    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=SimpleNamespace(),
        memory_governance=governance,
    )
    identity = TrustedHostIdentity(
        key_id="admin-key",
        audience="test",
        principal_type="user",
        tenant_id="tenant-1",
        user_id="admin-1",
        groups=(),
        credential_class="oac_admin",
        claims_version="oac-admin-principal-v1",
        roles=(),
        active_bundle_id="",
        policy_version="oac-control-v1",
        signature_version="v2",
        request_operation="memory-governance-read",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity

    response = non_lifespan_test_client(app).get(
        "/api/v1/admin/memories/governance?tenant_id=tenant-1&status=repairing&page=1&page_size=20"
    )

    assert response.status_code == 200
    assert governance.seen_status == "repairing"


@pytest.mark.parametrize("credential_class", ["oac_user", "coze_workflow"])
def test_oac_governance_query_rejects_non_admin_host_credentials(
    credential_class: str,
    non_lifespan_test_client,
) -> None:
    class GovernancePort:
        async def query(self, **_kwargs):
            raise AssertionError("non-admin credential reached governance service")

    ports = OacAdapterApplicationPorts(
        routing=SimpleNamespace(),
        registry=SimpleNamespace(),
        events=SimpleNamespace(),
        plans=SimpleNamespace(),
        delegated_runs=SimpleNamespace(),
        turns=SimpleNamespace(),
        memory_governance=GovernancePort(),
    )
    identity = TrustedHostIdentity(
        key_id="other-key",
        audience="test",
        principal_type="user" if credential_class == "oac_user" else "service",
        tenant_id="tenant-1",
        user_id="caller-1",
        groups=(),
        credential_class=credential_class,
        claims_version=(
            "oac-principal-v1" if credential_class == "oac_user" else "coze-workflow-v1"
        ),
        roles=(),
        active_bundle_id="",
        policy_version="oac-authz-v1",
        signature_version="v2",
        request_operation="memory-governance-read",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_oac_adapter_application_ports] = lambda: ports
    app.dependency_overrides[get_trusted_host_identity] = lambda: identity

    response = non_lifespan_test_client(app).get(
        "/api/v1/admin/memories/governance?tenant_id=tenant-1"
    )

    assert response.status_code == 403


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


@pytest.mark.asyncio
async def test_governance_canonical_close_retries_after_transient_lifecycle_failure() -> None:
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
            if self.calls == 1:
                raise RuntimeError("temporary database failure")
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

    with pytest.raises(RuntimeError, match="temporary database failure"):
        await service.repair(
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

    assert replay.accepted is True
    assert replay.action == "canonical_close"
    assert replay.idempotent_replay is True
    assert lifecycle.calls == 2
    assert (await service.query(tenant_id="tenant-1")).items == []


@pytest.mark.asyncio
async def test_canonical_close_claim_cannot_be_overwritten_by_another_idempotency_key() -> None:
    outbox = MemoryIndexOutboxRepository()
    operation = await outbox.add(
        MemoryIndexOperation(
            idempotency_key="delete",
            operation="delete",
            memory_id="unclosed",
            tenant_id="tenant-1",
            status="completed",
        )
    )

    claimed, replay = await outbox.accept_governance_repair(
        operation.index_operation_id,
        tenant_id="tenant-1",
        expected_status=operation.status,
        idempotency_key="repair-first",
        now=datetime(2026, 7, 29, tzinfo=UTC),
        requeue=False,
    )
    with pytest.raises(ValueError, match="already claimed"):
        await outbox.accept_governance_repair(
            operation.index_operation_id,
            tenant_id="tenant-1",
            expected_status=operation.status,
            idempotency_key="repair-second",
            now=datetime(2026, 7, 29, tzinfo=UTC),
            requeue=False,
        )
    replayed, is_replay = await outbox.accept_governance_repair(
        operation.index_operation_id,
        tenant_id="tenant-1",
        expected_status=operation.status,
        idempotency_key="repair-first",
        now=datetime(2026, 7, 29, tzinfo=UTC),
        requeue=False,
    )

    assert replay is False
    assert claimed.last_error_metadata["governance_repair_key"] == "repair-first"
    assert is_replay is True
    assert replayed.last_error_metadata["governance_repair_key"] == "repair-first"


@pytest.mark.asyncio
async def test_governance_replay_rejects_changed_request_preconditions() -> None:
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    await repository.add(item("dead", 301))
    await outbox.add(
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
        ),
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    target = (await service.query(tenant_id="tenant-1")).items[0]

    accepted = await service.repair(
        tenant_id="tenant-1",
        memory_id=target.memory_id,
        expected_version=target.version,
        expected_anomaly=target.anomaly,
        idempotency_key="repair-dead",
    )
    changed = await service.repair(
        tenant_id="tenant-1",
        memory_id=target.memory_id,
        expected_version="changed-version",
        expected_anomaly="provider_residual",
        idempotency_key="repair-dead",
    )

    assert accepted.accepted is True
    assert changed.accepted is False
