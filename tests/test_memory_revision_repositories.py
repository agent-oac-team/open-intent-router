import asyncio

import pytest

from app.core.config import Settings
from app.repositories.context_stores import DatabaseMemoryItemRepository, MemoryItemRepository
from app.repositories.memory_revisions import (
    DatabaseMemoryRevisionLedgerRepository,
    MemoryRevisionLedgerRepository,
)
from app.schemas.memory import MemoryItem, MemoryRevision


def _item() -> MemoryItem:
    return MemoryItem(
        memory_id="mem_1",
        memory_key="tenant:t1:user:u1:preference:language",
        scope="user_preference",
        subject_id="u1",
        user_id="u1",
        tenant_id="t1",
        content="中文",
        lifecycle_status="active",
    )


def _revision(content: str, operation: str = "add") -> MemoryRevision:
    return MemoryRevision(
        memory_id="mem_1",
        revision_no=1,
        memory_key="tenant:t1:user:u1:preference:language",
        operation=operation,
        content=content,
        policy_version="policy-v1",
    )


def _owner(**updates) -> dict[str, str]:
    return {
        "tenant_id": "t1",
        "user_id": "u1",
        "subject_type": "user",
        "subject_id": "u1",
        **updates,
    }


async def test_in_memory_revision_sequence_and_current_pointer() -> None:
    items = MemoryItemRepository()
    await items.add(_item())
    revisions = MemoryRevisionLedgerRepository(items)
    first, current = await revisions.append_revision_and_set_current(
        _revision("中文"), **_owner(), expected_current_revision_id=None
    )
    second, current = await revisions.append_revision_and_set_current(
        _revision("English", "update"),
        **_owner(),
        expected_current_revision_id=first.revision_id,
    )

    assert first.revision_no == 1
    assert second.revision_no == 2
    assert second.supersedes_revision_id == first.revision_id
    assert current.memory_id == "mem_1"
    assert current.current_revision_id == second.revision_id
    assert current.content == "English"
    pending = await items.list_active(index_statuses=["pending"])
    assert [item.memory_id for item in pending] == ["mem_1"]


async def test_in_memory_revision_history_cannot_be_mutated_through_returned_objects() -> None:
    items = MemoryItemRepository()
    await items.add(_item())
    revisions = MemoryRevisionLedgerRepository(items)
    returned_revision, returned_current = await revisions.append_revision_and_set_current(
        _revision("中文").model_copy(update={"structured_value": {"nested": {"language": "zh"}}}),
        **_owner(),
        expected_current_revision_id=None,
    )

    returned_revision.content = "tampered-return"
    returned_revision.structured_value["nested"]["language"] = "forged"
    returned_current.structured_value["nested"]["language"] = "forged-current"
    first_read = await revisions.list_for_memory("mem_1", **_owner())
    first_read[0].content = "tampered-list"
    first_read[0].structured_value["nested"]["language"] = "forged-list"
    second_read = await revisions.list_for_memory("mem_1", **_owner())

    assert second_read[0].content == "中文"
    assert second_read[0].structured_value == {"nested": {"language": "zh"}}
    assert items.items["mem_1"].structured_value == {"nested": {"language": "zh"}}


async def test_database_revision_sequence_concurrency_and_tenant_isolation(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'memory-revisions.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    items = DatabaseMemoryItemRepository(session_factory)
    await items.add(_item())
    revisions = DatabaseMemoryRevisionLedgerRepository(session_factory)
    first, _ = await revisions.append_revision_and_set_current(
        _revision("中文"), **_owner(), expected_current_revision_id=None
    )
    attempts = await asyncio.gather(
        revisions.append_revision_and_set_current(
            _revision("English", "update"),
            **_owner(),
            expected_current_revision_id=first.revision_id,
        ),
        revisions.append_revision_and_set_current(
            _revision("日本語", "update"),
            **_owner(),
            expected_current_revision_id=first.revision_id,
        ),
        return_exceptions=True,
    )
    successes = [value for value in attempts if not isinstance(value, Exception)]
    failures = [value for value in attempts if isinstance(value, ValueError)]
    assert len(successes) == 1
    assert len(failures) == 1
    stored = await revisions.list_for_memory("mem_1", **_owner())
    assert [revision.revision_no for revision in stored] == [1, 2]
    assert stored[1].supersedes_revision_id == first.revision_id
    assert await revisions.list_for_memory("mem_1", **_owner(tenant_id="t2")) == []
    with pytest.raises(ValueError, match="not found"):
        await revisions.append_revision_and_set_current(
            _revision("forged", "update"),
            **_owner(tenant_id="t2"),
            expected_current_revision_id=stored[1].revision_id,
        )


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_revision_repository_rejects_same_tenant_cross_subject_access(
    backend, tmp_path, managed_database
) -> None:
    if backend == "memory":
        items = MemoryItemRepository()
        revisions = MemoryRevisionLedgerRepository(items)
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'revision-owner.db'}",
        )
        await managed_database.initialize_schema(settings)
        session_factory = await managed_database.session_factory(settings)
        items = DatabaseMemoryItemRepository(session_factory)
        revisions = DatabaseMemoryRevisionLedgerRepository(session_factory)
    await items.add(_item())
    first, _ = await revisions.append_revision_and_set_current(
        _revision("中文"), **_owner(), expected_current_revision_id=None
    )

    forged_owners = [
        _owner(user_id="u2"),
        _owner(subject_id="u2"),
        _owner(subject_type="agent"),
    ]
    for forged_owner in forged_owners:
        assert await revisions.list_for_memory("mem_1", **forged_owner) == []
        with pytest.raises(ValueError, match="not found"):
            await revisions.append_revision_and_set_current(
                _revision("forged", "update"),
                **forged_owner,
                expected_current_revision_id=first.revision_id,
            )
