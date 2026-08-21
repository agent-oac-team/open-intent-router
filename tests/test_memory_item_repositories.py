from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.repositories.context_stores import DatabaseMemoryItemRepository, MemoryItemRepository
from app.schemas.memory import MemoryItem


def _memory(memory_id: str, **updates) -> MemoryItem:
    values = {
        "memory_id": memory_id,
        "memory_key": "tenant:t1:user:u1:preference:language",
        "scope": "user_preference",
        "subject_id": "u1",
        "user_id": "u1",
        "tenant_id": "t1",
        "content": memory_id,
        "lifecycle_status": "active",
        "index_status": "ready",
    }
    return MemoryItem(**{**values, **updates})


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_memory_item_repository_enforces_canonical_active_boundary(
    backend, tmp_path, managed_database
) -> None:
    if backend == "memory":
        repository = MemoryItemRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'memory-items.db'}",
        )
        await managed_database.initialize_schema(settings)
        repository = DatabaseMemoryItemRepository(await managed_database.session_factory(settings))
    active = await repository.add(_memory("active"))
    await repository.add(_memory("deleting", lifecycle_status="deletion_pending"))
    await repository.add(
        _memory(
            "expired",
            memory_key="tenant:t1:user:u1:preference:expired",
            ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await repository.add(_memory("other-tenant", tenant_id="t2"))
    await repository.add(_memory("agent-same-id", subject_type="agent"))
    await repository.add(
        _memory(
            "out-of-sync",
            memory_key="tenant:t1:user:u1:preference:tone",
            index_status="out_of_sync",
        )
    )

    current = await repository.get_current_by_key(
        tenant_id="t1",
        subject_type="user",
        subject_id="u1",
        scope="user_preference",
        memory_key=active.memory_key,
    )
    assert current and current.memory_id == "active"
    assert (
        await repository.get_current_by_key(
            tenant_id="t2",
            subject_type="user",
            subject_id="u1",
            scope="user_preference",
            memory_key=active.memory_key,
        )
        is not None
    )
    canonical = await repository.get_active_by_ids(
        ["deleting", "active", "expired", "other-tenant", "agent-same-id"],
        tenant_id="t1",
        subject_type="user",
        subject_id="u1",
        user_id="u1",
        scopes=["user_preference"],
    )
    assert [item.memory_id for item in canonical] == ["active"]
    ready = await repository.list_active(
        tenant_id="t1",
        subject_type="user",
        subject_id="u1",
        index_statuses=["ready"],
    )
    assert {item.memory_id for item in ready} == {"active"}
    deleting = await repository.list_active(
        tenant_id="t1",
        subject_type="user",
        subject_id="u1",
        lifecycle_statuses=["deletion_pending"],
    )
    assert [item.memory_id for item in deleting] == ["deleting"]
    forged = active.model_copy(
        update={
            "scope": "stable_fact",
            "memory_key": "tenant:t1:user:u1:fact:forged",
            "content": "forged",
        }
    )
    assert (
        await repository.compare_and_set_current(
            forged, expected_revision_id=active.current_revision_id
        )
        is False
    )

    with pytest.raises((ValueError, IntegrityError)):
        await repository.add(_memory("duplicate-active"))


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_memory_item_repository_does_not_expose_mutable_internal_state(
    backend, tmp_path, managed_database
) -> None:
    if backend == "memory":
        repository = MemoryItemRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'memory-item-alias.db'}",
        )
        await managed_database.initialize_schema(settings)
        repository = DatabaseMemoryItemRepository(await managed_database.session_factory(settings))
    original = _memory("active", structured_value={"nested": {"value": "original"}})
    returned = await repository.add(original)
    original.tenant_id = "attacker-tenant"
    returned.user_id = "attacker-user"
    returned.structured_value["nested"]["value"] = "attacker"

    current = await repository.get_current_by_key(
        tenant_id="t1",
        subject_type="user",
        subject_id="u1",
        scope="user_preference",
        memory_key="tenant:t1:user:u1:preference:language",
    )
    assert current
    assert current.tenant_id == "t1"
    assert current.user_id == "u1"
    assert current.structured_value == {"nested": {"value": "original"}}
    current.content = "updated"
    assert await repository.compare_and_set_current(current, expected_revision_id=None)
    current.content = "tampered-after-cas"
    reloaded = await repository.get_active_by_ids(
        ["active"],
        tenant_id="t1",
        subject_type="user",
        subject_id="u1",
        user_id="u1",
    )
    assert reloaded[0].content == "updated"
    reloaded[0].lifecycle_status = "deletion_pending"
    assert (await repository.list_active(tenant_id="t1"))[0].lifecycle_status == "active"
