import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.repositories.memory_index_operations import (
    DatabaseMemoryIndexOutboxRepository,
    MemoryIndexOutboxRepository,
)
from app.schemas.memory import MemoryIndexOperation


def _operation(**updates) -> MemoryIndexOperation:
    values = {
        "idempotency_key": "mem_1:rev_1:add",
        "operation": "add",
        "memory_id": "mem_1",
        "revision_id": "rev_1",
        "tenant_id": "t1",
    }
    return MemoryIndexOperation(**{**values, **updates})


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_index_outbox_claim_retry_complete_and_repair(
    backend, tmp_path, managed_database
) -> None:
    if backend == "memory":
        repository = MemoryIndexOutboxRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'index-outbox.db'}",
        )
        await managed_database.initialize_schema(settings)
        repository = DatabaseMemoryIndexOutboxRepository(
            await managed_database.session_factory(settings)
        )
    stored = await repository.add(_operation())
    duplicate = await repository.add(_operation(index_operation_id="duplicate"))
    assert duplicate.index_operation_id == stored.index_operation_id
    with pytest.raises(ValueError, match="identity conflict"):
        await repository.add(_operation(tenant_id="t2"))

    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claimed = await repository.claim(owner="worker-a", now=now, lease_seconds=30)
    assert claimed and claimed.attempt_count == 1
    failed = await repository.fail(
        claimed.index_operation_id,
        owner="worker-a",
        lease_token=claimed.lease_token,
        now=now + timedelta(seconds=1),
        error_code="provider_timeout",
        next_attempt_at=now + timedelta(seconds=5),
    )
    assert failed.status == "retry"
    repair = await repository.list_repair_candidates(tenant_id="t1", statuses=["retry"])
    assert [item.index_operation_id for item in repair] == [stored.index_operation_id]
    assert await repository.list_repair_candidates(tenant_id="t2", statuses=["retry"]) == []
    reclaimed = await repository.claim(
        owner="worker-b", now=now + timedelta(seconds=5), lease_seconds=30
    )
    assert reclaimed
    completed = await repository.complete(
        reclaimed.index_operation_id,
        owner="worker-b",
        lease_token=reclaimed.lease_token,
        now=now + timedelta(seconds=6),
        external_memory_id="mem0_1",
        result_metadata={"provider_status": "not_found"},
    )
    assert completed.status == "completed"
    assert completed.external_memory_id == "mem0_1"
    assert completed.last_error_metadata == {"provider_status": "not_found"}


async def test_database_index_claim_and_terminal_transitions_are_atomic(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'index-outbox-race.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseMemoryIndexOutboxRepository(
        await managed_database.session_factory(settings)
    )
    stored = await repository.add(_operation())
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claims = await asyncio.gather(
        repository.claim(owner="worker-a", now=now, lease_seconds=30),
        repository.claim(owner="worker-b", now=now, lease_seconds=30),
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    claim = winners[0]
    terminal = await asyncio.gather(
        repository.complete(
            stored.index_operation_id,
            owner=claim.lease_owner,
            lease_token=claim.lease_token,
            now=now + timedelta(seconds=1),
        ),
        repository.fail(
            stored.index_operation_id,
            owner=claim.lease_owner,
            lease_token=claim.lease_token,
            now=now + timedelta(seconds=1),
            error_code="error",
            next_attempt_at=now + timedelta(seconds=5),
        ),
        return_exceptions=True,
    )
    assert len([value for value in terminal if not isinstance(value, Exception)]) == 1
    assert len([value for value in terminal if isinstance(value, ValueError)]) == 1


async def test_database_complete_without_external_id_preserves_mapping(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'index-mapping.db'}",
    )
    await managed_database.initialize_schema(settings)
    repository = DatabaseMemoryIndexOutboxRepository(
        await managed_database.session_factory(settings)
    )
    await repository.add(_operation(external_memory_id="mem0_existing"))
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claim = await repository.claim(owner="worker", now=now, lease_seconds=30)
    assert claim
    completed = await repository.complete(
        claim.index_operation_id,
        owner="worker",
        lease_token=claim.lease_token,
        now=now + timedelta(seconds=1),
    )
    assert completed.external_memory_id == "mem0_existing"


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_index_claim_can_be_scoped_to_one_tenant(backend, tmp_path, managed_database) -> None:
    if backend == "memory":
        repository = MemoryIndexOutboxRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'index-tenant-claim.db'}",
        )
        await managed_database.initialize_schema(settings)
        repository = DatabaseMemoryIndexOutboxRepository(
            await managed_database.session_factory(settings)
        )
    await repository.add(
        _operation(
            index_operation_id="operation_tenant_2",
            idempotency_key="tenant-2-operation",
            memory_id="memory_tenant_2",
            tenant_id="t2",
        )
    )
    await repository.add(_operation(index_operation_id="operation_tenant_1"))
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)

    claimed = await repository.claim(
        owner="tenant-worker",
        now=now,
        lease_seconds=30,
        tenant_id="t1",
    )

    assert claimed is not None and claimed.tenant_id == "t1"
    untouched = await repository.get("operation_tenant_2", tenant_id="t2")
    assert untouched is not None and untouched.status == "pending"


async def test_in_memory_index_outbox_does_not_expose_mutable_state() -> None:
    repository = MemoryIndexOutboxRepository()
    original = _operation(last_error_metadata={"nested": {"value": "original"}})
    returned = await repository.add(original)
    original.tenant_id = "attacker-tenant"
    returned.last_error_metadata["nested"]["value"] = "attacker"
    now = datetime(2026, 7, 13, 1, tzinfo=UTC)
    claimed = await repository.claim(owner="worker", now=now, lease_seconds=30)
    assert claimed and claimed.tenant_id == "t1"
    assert claimed.last_error_metadata == {"nested": {"value": "original"}}
    lease_token = claimed.lease_token
    claimed.lease_token = "attacker-token"
    completed = await repository.complete(
        claimed.index_operation_id,
        owner="worker",
        lease_token=lease_token,
        now=now + timedelta(seconds=1),
    )
    completed.tenant_id = "attacker-tenant"
    stored = repository.operations[completed.index_operation_id]
    assert stored.tenant_id == "t1"
