import asyncio
import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from app.core.config import Settings
from app.db.models import (
    MemoryEventModel,
    MemoryFormationJobModel,
    MemoryFormationTurnModel,
    MemoryIndexOperationModel,
    MemoryItemModel,
    MemoryRevisionModel,
)
from app.repositories.context_stores import DatabaseMemoryItemRepository, MemoryItemRepository
from app.repositories.json_utils import dumps, loads
from app.repositories.memory_formation import (
    DatabaseMemoryFormationTurnJobRepository,
    MemoryFormationTurnJobRepository,
)
from app.repositories.memory_index_operations import (
    DatabaseMemoryIndexOutboxRepository,
    MemoryIndexOutboxRepository,
)
from app.repositories.memory_lifecycle_store import (
    DatabaseMemoryLifecycleStore,
    MemoryLifecycleStore,
)
from app.repositories.memory_revisions import MemoryRevisionLedgerRepository
from app.schemas.common import UserContext
from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryDecisionStatus,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationTurn,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryRecallRequest,
)
from app.services.memory_candidate_policy import CandidatePolicyResult, MemoryCandidatePolicy
from app.services.memory_lifecycle import (
    MemoryConsolidationService,
    MemoryLifecycleService,
    MemoryTtlSweeper,
)
from app.services.memory_service import MemoryService


async def _backend(tmp_path, backend: str, managed_database=None, *, invalidations=None):
    settings = Settings(storage_backend="memory")
    if backend == "memory":
        items = MemoryItemRepository()
        revisions = MemoryRevisionLedgerRepository(items)
        index = MemoryIndexOutboxRepository()
        formation = MemoryFormationTurnJobRepository()
        store = MemoryLifecycleStore(
            item_repository=items,
            revision_repository=revisions,
            index_repository=index,
            formation_repository=formation,
        )
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / f'lifecycle-{backend}.db'}",
        )
        assert managed_database is not None
        await managed_database.initialize_schema(settings)
        session_factory = await managed_database.session_factory(settings)
        items = DatabaseMemoryItemRepository(session_factory)
        revisions = None
        index = None
        formation = None
        store = DatabaseMemoryLifecycleStore(session_factory)

    async def invalidate(tenant_id: str, memory_id: str) -> None:
        if invalidations is not None:
            invalidations.append((tenant_id, memory_id))

    service = MemoryLifecycleService(
        settings=settings,
        store=store,
        cache_invalidator=invalidate,
        clock=lambda: datetime(2026, 7, 13, 8, tzinfo=UTC),
    )
    return service, store, items, revisions, index, formation


def _candidate(value: str = "zh", **updates) -> MemoryFormationCandidate:
    values = {
        "candidate_id": f"candidate_{value}",
        "proposed_operation": "add",
        "scope": "user_preference",
        "content": f"User prefers {value}",
        "structured_value": {"slot": "response_language", "value": value},
        "confidence": 0.95,
        "importance": 0.8,
        "evidence_refs": [MemoryEvidenceRef(turn_id="turn_1", role="user", quote=f"use {value}")],
    }
    return MemoryFormationCandidate(**{**values, **updates})


def _operation(kind: str, **updates) -> MemoryLifecycleOperation:
    status = {
        "add": MemoryDecisionStatus.ACCEPTED,
        "update": MemoryDecisionStatus.ACCEPTED,
        "delete": MemoryDecisionStatus.ACCEPTED,
        "noop": MemoryDecisionStatus.NOOP,
        "reject": MemoryDecisionStatus.REJECTED,
        "pending": MemoryDecisionStatus.PENDING,
    }[kind]
    reasons = {
        "add": "accepted_new",
        "update": "accepted_update",
        "delete": "authorized_delete",
        "noop": "same_value",
        "reject": "confidence_low",
        "pending": "confidence_pending",
    }
    values = {
        "operation_id": f"operation_{kind}",
        "operation": kind,
        "decision_status": status,
        "reason_code": reasons[kind],
        "tenant_id": "tenant_1",
        "user_id": "user_1",
        "subject_type": "user",
        "subject_id": "user_1",
        "memory_key": "tenant:tenant_1:user:user_1:preference:response_language",
        "candidate_hash": f"sha256:{kind}",
        "formation_job_id": "job_1",
        "canonical_refs": ["turn_1"],
    }
    return MemoryLifecycleOperation(**{**values, **updates})


def _result(operation: MemoryLifecycleOperation, candidate=None) -> CandidatePolicyResult:
    return CandidatePolicyResult(
        candidate=candidate or _candidate(), operation=operation, redacted_trace={}
    )


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_lifecycle_add_update_revision_and_idempotency(
    backend, tmp_path, managed_database
) -> None:
    service, store, items, revisions, index, _ = await _backend(tmp_path, backend, managed_database)
    added = await service.apply(_result(_operation("add")))
    replay = await service.apply(_result(_operation("add")))

    assert added.item and added.revision and added.index_operation and added.event
    assert added.revision.revision_no == 1
    assert added.item.current_revision_id == added.revision.revision_id
    assert added.item.index_status == "pending"
    assert replay.idempotent_replay is True
    assert replay.item.memory_id == added.item.memory_id

    update_operation = _operation(
        "update",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    updated = await service.apply(
        _result(
            update_operation,
            _candidate(
                "en",
                proposed_operation="update",
                structured_value={
                    "slot": "response_language",
                    "value": "en",
                    "change_intent": "explicit_long_term",
                },
            ),
        )
    )
    assert updated.item.memory_id == added.item.memory_id
    assert updated.revision.revision_no == 2
    assert updated.revision.supersedes_revision_id == added.revision.revision_id
    assert updated.index_operation.operation == "update"
    assert updated.item.content == "User prefers en"

    if backend == "memory":
        assert len(items.items) == 1
        assert len(revisions.revisions[added.item.memory_id]) == 2
        assert len(index.operations) == 2


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_concurrent_update_keeps_one_current_revision(
    backend, tmp_path, managed_database
) -> None:
    service, store, *_ = await _backend(tmp_path, backend, managed_database)
    added = await service.apply(_result(_operation("add")))
    base = _operation(
        "update",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    attempts = await asyncio.gather(
        service.apply(
            _result(
                base.model_copy(update={"operation_id": "update_a"}),
                _candidate("en", proposed_operation="update"),
            )
        ),
        service.apply(
            _result(
                base.model_copy(update={"operation_id": "update_b"}),
                _candidate("fr", proposed_operation="update"),
            )
        ),
        return_exceptions=True,
    )
    assert len([value for value in attempts if not isinstance(value, Exception)]) == 1
    assert len([value for value in attempts if isinstance(value, ValueError)]) == 1


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_non_mutating_decisions_persist_without_provider_side_effects(
    backend, tmp_path, managed_database
) -> None:
    service, _, items, _, index, _ = await _backend(tmp_path, backend, managed_database)
    results = [
        await service.apply(_result(_operation(kind))) for kind in ("noop", "reject", "pending")
    ]
    assert [result.event.decision_status for result in results] == [
        "noop",
        "rejected",
        "pending",
    ]
    assert all(result.index_operation is None for result in results)
    if backend == "memory":
        assert items.items == {}
        assert index.operations == {}


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_delete_is_fail_closed_then_hard_deletes_content(
    backend, tmp_path, managed_database
) -> None:
    invalidations = []
    service, store, items, revisions, index, _ = await _backend(
        tmp_path, backend, managed_database, invalidations=invalidations
    )
    added = await service.apply(_result(_operation("add")))
    delete = _operation(
        "delete",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    requested = await service.request_delete(delete)
    replay = await service.request_delete(delete)
    assert requested.item.lifecycle_status == "deletion_pending"
    assert requested.item.index_status == "deletion_pending"
    assert requested.index_operation.operation == "delete"
    assert replay.idempotent_replay is True
    assert (
        await items.get_active_by_ids(
            [added.item.memory_id],
            tenant_id="tenant_1",
            user_id="user_1",
            subject_type="user",
            subject_id="user_1",
        )
        == []
    )

    with pytest.raises(ValueError, match="Provider delete operation is not completed"):
        await service.complete_delete(delete)
    index_repository = (
        index if backend == "memory" else DatabaseMemoryIndexOutboxRepository(store.session_factory)
    )
    while True:
        claimed = await index_repository.claim(
            owner="delete-worker",
            now=datetime(2026, 7, 13, 8, tzinfo=UTC),
            lease_seconds=30,
        )
        assert claimed
        await index_repository.complete(
            claimed.index_operation_id,
            owner="delete-worker",
            lease_token=claimed.lease_token,
            now=datetime(2026, 7, 13, 8, 0, 1, tzinfo=UTC),
        )
        if claimed.index_operation_id == requested.index_operation.index_operation_id:
            break
    completed = await service.complete_delete(delete)
    assert completed.event.event_type == "memory_deleted_tombstone"
    assert completed.event.memory_key is None
    assert "content" not in completed.event.payload
    assert await store.get_item(added.item.memory_id) is None
    assert invalidations == [
        ("tenant_1", added.item.memory_id),
        ("tenant_1", added.item.memory_id),
        ("tenant_1", added.item.memory_id),
    ]
    if backend == "memory":
        assert revisions.revisions.get(added.item.memory_id) is None


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_delete_failure_never_reactivates_and_dead_letter_scrubs_ledger(
    backend, tmp_path, managed_database
) -> None:
    service, store, *_ = await _backend(tmp_path, backend, managed_database)
    added = await service.apply(_result(_operation("add")))
    delete = _operation(
        "delete",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    await service.request_delete(delete)
    retry = await service.record_delete_failure(
        delete, error_code="provider_timeout", dead_letter=False
    )
    assert retry.item.lifecycle_status == "deletion_pending"
    assert retry.item.content
    assert retry.event.payload["reason_code"] == "provider_error"
    dead = await service.record_delete_failure(
        delete, error_code="provider_unavailable", dead_letter=True
    )
    assert dead.item.lifecycle_status == "deletion_pending"
    assert dead.item.index_status == "dead_letter"
    assert dead.item.content == ""
    assert dead.item.structured_value == {}
    assert dead.event.payload["reason_code"] == "provider_error"


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_ttl_sweeper_uses_same_deletion_path(backend, tmp_path, managed_database) -> None:
    service, store, *_ = await _backend(tmp_path, backend, managed_database)
    added = await service.apply(
        _result(
            _operation("add"),
            _candidate(
                scope="task_memory",
                memory_key_hint="task_status",
                structured_value={"slot": "task_status"},
            ),
        )
    )
    if backend == "memory":
        store.item_repository.items[added.item.memory_id] = added.item.model_copy(
            update={"ttl_expires_at": datetime(2020, 1, 1)}
        )
    else:
        async with store.session_factory() as session:
            from sqlalchemy import update

            from app.db.models import MemoryItemModel

            await session.execute(
                update(MemoryItemModel)
                .where(MemoryItemModel.memory_id == added.item.memory_id)
                .values(ttl_expires_at=datetime(2020, 1, 1))
            )
            await session.commit()
    sweeper = MemoryTtlSweeper(
        lifecycle=service,
        store=store,
        clock=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    results = await sweeper.run_once()
    assert len(results) == 1
    assert results[0].operation.reason_code == "ttl_expired"
    assert results[0].item.lifecycle_status == "deletion_pending"


async def test_delete_scrubs_directly_associated_pending_capsules() -> None:
    invalidations = []
    service, store, _, _, _, formation = await _backend(None, "memory", invalidations=invalidations)
    added = await service.apply(_result(_operation("add")))
    turn = MemoryFormationTurn(
        turn_id="turn_1",
        request_id="request_1",
        session_id="session_1",
        user_id="user_1",
        tenant_id="tenant_1",
        user_text="sensitive source text",
        assistant_text="sensitive assistant text",
        result_status="success",
        used_memory_ids=[added.item.memory_id],
    )
    await formation.append_turn(turn)
    job = MemoryFormationJob(
        job_id="pending_job",
        trigger="manual",
        mode="enforced",
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        source_refs=["turn_1"],
        idempotency_key="pending-job",
        model_version="v1",
        prompt_version="v1",
        policy_version="v1",
        trace_summary={"content": "sensitive source text", "memory_id": added.item.memory_id},
    )
    formation.jobs[job.job_id] = job
    formation.jobs_by_idempotency[job.idempotency_key] = job.job_id
    delete = _operation(
        "delete",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    await service.request_delete(delete)

    assert formation.turns["turn_1"].user_text == ""
    assert formation.turns["turn_1"].assistant_text == ""
    assert formation.turns["turn_1"].used_memory_ids == []
    assert formation.jobs["pending_job"].trace_summary["content"] == "[redacted]"


async def test_database_delete_scrubs_associated_capsule_and_job_payload(
    tmp_path, managed_database
) -> None:
    service, store, *_ = await _backend(tmp_path, "database", managed_database)
    added = await service.apply(_result(_operation("add")))
    formation = DatabaseMemoryFormationTurnJobRepository(store.session_factory)
    await formation.append_turn(
        MemoryFormationTurn(
            turn_id="turn_1",
            request_id="request_1",
            session_id="session_1",
            user_id="user_1",
            tenant_id="tenant_1",
            user_text="sensitive source text",
            assistant_text="sensitive assistant text",
            result_status="success",
            used_memory_ids=[added.item.memory_id],
        )
    )
    job = await formation.create_job_for_pending(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        trigger="manual",
        mode="enforced",
        model_version="v1",
        prompt_version="v1",
        policy_version="v1",
    )
    assert job
    async with store.session_factory() as session:
        row = await session.get(MemoryFormationJobModel, job.job_id)
        row.trace_summary_text = dumps(
            {"content": "sensitive source text", "memory_id": added.item.memory_id}
        )
        await session.commit()

    delete = _operation(
        "delete",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    await service.request_delete(delete)

    async with store.session_factory() as session:
        turn_row = await session.get(MemoryFormationTurnModel, "turn_1")
        job_row = await session.get(MemoryFormationJobModel, job.job_id)
        assert turn_row.user_text == ""
        assert turn_row.assistant_text == ""
        assert loads(turn_row.used_memory_ids_text, []) == []
        assert loads(job_row.trace_summary_text, {})["content"] == "[redacted]"


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_concurrent_ttl_sweepers_create_one_delete_operation(
    backend, tmp_path, managed_database
) -> None:
    service, store, *_ = await _backend(tmp_path, backend, managed_database)
    added = await service.apply(_result(_operation("add")))
    if backend == "memory":
        store.item_repository.items[added.item.memory_id] = added.item.model_copy(
            update={"ttl_expires_at": datetime(2020, 1, 1)}
        )
    else:
        async with store.session_factory() as session:
            await session.execute(
                update(MemoryItemModel)
                .where(MemoryItemModel.memory_id == added.item.memory_id)
                .values(ttl_expires_at=datetime(2020, 1, 1))
            )
            await session.commit()
    sweeper_a = MemoryTtlSweeper(
        lifecycle=service,
        store=store,
        clock=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    sweeper_b = MemoryTtlSweeper(
        lifecycle=service,
        store=store,
        clock=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    results = await asyncio.gather(sweeper_a.run_once(), sweeper_b.run_once())
    if backend == "memory":
        operations = store.index_repository.operations.values()
    else:
        operations = await DatabaseMemoryIndexOutboxRepository(
            store.session_factory
        ).list_repair_candidates(
            tenant_id="tenant_1",
            statuses=["pending", "claimed", "retry", "completed", "dead_letter"],
        )
    delete_operations = [operation for operation in operations if operation.operation == "delete"]
    assert sum(len(value) for value in results) in {1, 2}
    assert len(delete_operations) == 1


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_replay_rejects_reused_operation_id_from_another_owner(
    backend, tmp_path, managed_database
) -> None:
    service, *_ = await _backend(tmp_path, backend, managed_database)
    added = await service.apply(_result(_operation("add")))
    forged_add = _operation(
        "add",
        tenant_id="tenant_2",
        user_id="user_2",
        subject_id="user_2",
        memory_key="tenant:tenant_2:user:user_2:preference:response_language",
    )
    with pytest.raises(ValueError, match="replay identity conflict"):
        await service.apply(_result(forged_add))

    delete = _operation(
        "delete",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    await service.request_delete(delete)
    forged_delete = delete.model_copy(
        update={
            "tenant_id": "tenant_2",
            "user_id": "user_2",
            "subject_id": "user_2",
            "memory_key": "tenant:tenant_2:user:user_2:preference:response_language",
        }
    )
    with pytest.raises(ValueError, match="not found|replay identity conflict"):
        await service.request_delete(forged_delete)


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_concurrent_hard_delete_converges_to_same_tombstone(
    backend, tmp_path, managed_database
) -> None:
    service, store, *_, index, _ = await _backend(tmp_path, backend, managed_database)
    added = await service.apply(_result(_operation("add")))
    delete = _operation(
        "delete",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    requested = await service.request_delete(delete)
    index_repository = (
        index if backend == "memory" else DatabaseMemoryIndexOutboxRepository(store.session_factory)
    )
    while True:
        claimed = await index_repository.claim(
            owner="delete-worker",
            now=datetime(2026, 7, 13, 8, tzinfo=UTC),
            lease_seconds=30,
        )
        assert claimed
        await index_repository.complete(
            claimed.index_operation_id,
            owner="delete-worker",
            lease_token=claimed.lease_token,
            now=datetime(2026, 7, 13, 8, 0, 1, tzinfo=UTC),
        )
        if claimed.index_operation_id == requested.index_operation.index_operation_id:
            break
    completed = await asyncio.gather(
        service.complete_delete(delete), service.complete_delete(delete)
    )
    assert {result.event.event_id for result in completed} == {completed[0].event.event_id}
    assert sum(result.idempotent_replay for result in completed) == 1
    forged = delete.model_copy(
        update={
            "subject_type": "agent",
            "subject_id": "forged_agent",
            "memory_key": "tenant:tenant_1:agent:forged:key",
            "candidate_hash": "sha256:forged",
        }
    )
    with pytest.raises(ValueError, match="replay identity conflict"):
        await service.complete_delete(forged)


async def test_cleanup_queues_ttl_delete_without_bypassing_revision_ledger() -> None:
    service, store, items, revisions, index, _ = await _backend(None, "memory")
    added = await service.apply(
        _result(
            _operation("add"),
            _candidate(
                scope="task_memory",
                content="private task body",
                structured_value={"slot": "task_status"},
                memory_key_hint="task_status",
            ),
        )
    )
    store.item_repository.items[added.item.memory_id] = added.item.model_copy(
        update={"ttl_expires_at": datetime(2020, 1, 1)}
    )

    class Adapter:
        def __init__(self):
            self.deletes = []

        async def delete_many(self, memory_ids, *, items=None):
            self.deletes.append((memory_ids, items))

    adapter = Adapter()
    memory_service = MemoryService(
        Settings(storage_backend="memory"),
        repository=items,
        adapter=adapter,
        ttl_sweeper=MemoryTtlSweeper(
            lifecycle=service,
            store=store,
            clock=lambda: datetime(2026, 7, 13, tzinfo=UTC),
        ),
    )
    cleanup = await memory_service.cleanup_expired()
    assert cleanup.expired_memory_ids == [added.item.memory_id]
    assert items.items[added.item.memory_id].lifecycle_status == "deletion_pending"
    assert revisions.revisions[added.item.memory_id][0].content == "private task body"
    assert adapter.deletes == []
    assert (
        len(
            [
                operation
                for operation in index.operations.values()
                if operation.operation == "delete"
            ]
        )
        == 1
    )


async def test_consolidation_discovers_partitioned_duplicates_and_summaries() -> None:
    service, store, items, *_ = await _backend(None, "memory")
    now = datetime(2026, 7, 13, tzinfo=UTC)

    async def add_item(memory_id, scope, key, content, structured=None):
        return await items.add(
            MemoryItem(
                memory_id=memory_id,
                scope=scope,
                subject_id="user_1",
                user_id="user_1",
                tenant_id="tenant_1",
                memory_key=key,
                content=content,
                structured_value=structured or {},
                created_at=now,
                updated_at=now,
            )
        )

    duplicate_a = await add_item(
        "duplicate_a",
        "user_preference",
        "tenant:tenant_1:user:user_1:preference:format_a",
        "prefers concise answers",
        {"slot": "response_format", "value": "concise"},
    )
    duplicate_b = await add_item(
        "duplicate_b",
        "user_preference",
        "tenant:tenant_1:user:user_1:preference:format_b",
        "prefers concise answers",
        {"slot": "response_format", "value": "concise"},
    )
    fact_a = await add_item(
        "fact_a",
        "stable_fact",
        "tenant:tenant_1:user:user_1:fact:home_a",
        "Lives in Paris",
        {"city": "Paris"},
    )
    fact_b = await add_item(
        "fact_b",
        "stable_fact",
        "tenant:tenant_1:user:user_1:fact:home_b",
        "Paris residence",
        {"city": "Paris", "source": "other"},
    )
    summary_a = await add_item(
        "summary_a",
        "session_summary",
        "tenant:tenant_1:user:user_1:session_summary:s1",
        "First bounded summary",
    )
    summary_b = await add_item(
        "summary_b",
        "session_summary",
        "tenant:tenant_1:user:user_1:session_summary:s2",
        "Second bounded summary",
    )
    await items.add(
        duplicate_a.model_copy(
            update={
                "memory_id": "other_tenant",
                "tenant_id": "tenant_2",
                "user_id": "user_2",
                "subject_id": "user_2",
                "memory_key": "tenant:tenant_2:user:user_2:preference:format",
            }
        )
    )
    loader_calls = []

    async def semantic_finder(_partition):
        return [(fact_a.memory_id, fact_b.memory_id, 0.85)]

    async def canonical_loader(tenant_id, user_id):
        loader_calls.append((tenant_id, user_id))
        return []

    job = MemoryFormationJob(
        job_id="discovery_job",
        trigger="consolidation",
        mode="enforced",
        tenant_id="tenant_1",
        user_id="user_1",
        source_refs=["consolidation_event"],
        idempotency_key="discovery-job",
        model_version="v1",
        prompt_version="v1",
        policy_version="v1",
    )
    policy = MemoryCandidatePolicy(settings=Settings(storage_backend="memory"), repository=items)
    consolidator = MemoryConsolidationService(
        policy=policy,
        lifecycle=service,
        semantic_duplicate_finder=semantic_finder,
        canonical_task_loader=canonical_loader,
    )
    results = await consolidator.run_partition(job=job)
    operations = [result.operation.operation for result in results]
    assert operations.count("delete") == 2
    assert operations.count("pending") == 1
    assert operations.count("consolidate") == 1
    assert items.items[duplicate_b.memory_id].lifecycle_status == "deletion_pending"
    assert items.items[summary_b.memory_id].lifecycle_status == "deletion_pending"
    assert "First bounded summary" in items.items[summary_a.memory_id].content
    assert "Second bounded summary" in items.items[summary_a.memory_id].content
    assert items.items["other_tenant"].lifecycle_status == "active"
    assert loader_calls == [("tenant_1", "user_1")]
    assert (
        len(
            [
                operation
                for operation in store.index_repository.operations.values()
                if operation.operation == "delete"
            ]
        )
        == 2
    )


@pytest.mark.parametrize("self_reported_kind", [None, "exact_duplicate", "semantic_duplicate"])
async def test_consolidation_summary_cannot_be_promoted_to_stable_fact(
    self_reported_kind,
) -> None:
    _, _, items, *_ = await _backend(None, "memory")
    policy = MemoryCandidatePolicy(settings=Settings(storage_backend="memory"), repository=items)
    job = MemoryFormationJob(
        job_id="summary_promotion_job",
        trigger="consolidation",
        mode="enforced",
        tenant_id="tenant_1",
        user_id="user_1",
        source_refs=["summary_event"],
        idempotency_key="summary-promotion",
        model_version="v1",
        prompt_version="v1",
        policy_version="v1",
    )
    structured = {"slot": "home_city", "value": "Paris"}
    if self_reported_kind:
        structured["consolidation_kind"] = self_reported_kind
    candidate = _candidate(
        scope="stable_fact",
        content="User lives in Paris",
        structured_value=structured,
        memory_key_hint="home_city",
        evidence_refs=[MemoryEvidenceRef(event_id="summary_event", role="canonical")],
    )
    result = await policy.evaluate(job=job, candidate=candidate, turns=[])
    assert result.operation.operation == "reject"
    assert result.operation.reason_code == "invalid_evidence"


async def test_database_add_rolls_back_every_canonical_write_on_outbox_conflict(
    tmp_path,
    managed_database,
) -> None:
    service, store, *_ = await _backend(tmp_path, "database", managed_database)
    revision_id = "mrev_" + hashlib.sha256(b"operation_add:revision:1").hexdigest()[:32]
    identity = f"operation_add:add:{revision_id}"
    idempotency_key = f"lifecycle:{hashlib.sha256(identity.encode()).hexdigest()}"
    async with store.session_factory() as session:
        session.add(
            MemoryIndexOperationModel(
                index_operation_id="conflicting_outbox",
                idempotency_key=idempotency_key,
                operation="add",
                memory_id="other_memory",
                revision_id="other_revision",
                tenant_id="tenant_1",
                status="pending",
                attempt_count=0,
                max_attempts=5,
                last_error_metadata_text="{}",
            )
        )
        await session.commit()

    with pytest.raises(ValueError, match="ADD conflict"):
        await service.apply(_result(_operation("add")))

    async with store.session_factory() as session:
        from sqlalchemy import func, select

        assert await session.scalar(select(func.count()).select_from(MemoryItemModel)) == 0
        assert await session.scalar(select(func.count()).select_from(MemoryRevisionModel)) == 0
        assert await session.scalar(select(func.count()).select_from(MemoryEventModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(MemoryIndexOperationModel)) == 1
        )


async def test_lifecycle_rejects_cross_tenant_and_stale_revision_updates() -> None:
    service, *_ = await _backend(None, "memory")
    added = await service.apply(_result(_operation("add")))
    candidate = _candidate("en", proposed_operation="update")
    forged = _operation(
        "update",
        tenant_id="tenant_2",
        memory_id=added.item.memory_id,
        revision_id=added.revision.revision_id,
    )
    stale = _operation(
        "update",
        operation_id="stale_update",
        memory_id=added.item.memory_id,
        revision_id="stale_revision",
    )

    with pytest.raises(ValueError, match="not found"):
        await service.apply(_result(forged, candidate))
    with pytest.raises(ValueError, match="precondition"):
        await service.apply(_result(stale, candidate))


async def test_consolidation_reuses_policy_and_revision_paths() -> None:
    service, _, items, *_ = await _backend(None, "memory")
    preference = await service.apply(_result(_operation("add")))
    task_key = "tenant:tenant_1:user:user_1:plan:plan_1:task_status"
    task_candidate = _candidate(
        proposed_operation="add",
        scope="task_memory",
        content="Plan pending",
        structured_value={
            "object_type": "plan",
            "plan_id": "plan_1",
            "status": "pending",
        },
        memory_key_hint=task_key,
        evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
    )
    task = await service.apply(
        _result(_operation("add", operation_id="task_add", memory_key=task_key), task_candidate)
    )
    job = MemoryFormationJob(
        job_id="consolidation_job",
        trigger="consolidation",
        mode="enforced",
        tenant_id="tenant_1",
        user_id="user_1",
        source_refs=["event_1"],
        idempotency_key="consolidation-job",
        model_version="v1",
        prompt_version="v1",
        policy_version="v1",
    )
    candidates = [
        task_candidate.model_copy(
            update={
                "candidate_id": "stale_task",
                "proposed_operation": MemoryCandidateOperation.UPDATE,
                "content": "Plan completed",
                "structured_value": {
                    "object_type": "plan",
                    "plan_id": "plan_1",
                    "status": "completed",
                },
                "target_memory_id": task.item.memory_id,
            }
        ),
        _candidate(
            structured_value={
                "slot": "response_language",
                "value": "zh",
                "consolidation_kind": "exact_duplicate",
            },
            evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
        ),
        _candidate(
            "fr",
            candidate_id="semantic_conflict",
            proposed_operation="update",
            structured_value={
                "slot": "response_language",
                "value": "fr",
                "consolidation_kind": "semantic_duplicate",
            },
            evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
        ),
        _candidate(
            candidate_id="session_summary",
            scope="session_summary",
            content="Bounded session summary",
            structured_value={"slot": "session_1"},
            memory_key_hint="session_1",
            evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
        ),
        _candidate(
            candidate_id="cross_tenant",
            tenant_id_hint="tenant_2",
            evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
        ),
    ]
    policy = MemoryCandidatePolicy(settings=Settings(storage_backend="memory"), repository=items)
    consolidator = MemoryConsolidationService(policy=policy, lifecycle=service)
    results = await consolidator.process(job=job, candidates=candidates, turns=[])

    assert [result.operation.operation for result in results] == [
        "consolidate",
        "reject",
        "reject",
        "add",
        "reject",
    ]
    assert results[0].revision.operation == "consolidate"
    assert results[0].revision.supersedes_revision_id == task.revision.revision_id
    assert results[1].item is None and results[1].index_operation is None
    assert results[2].item is None and results[2].index_operation is None
    assert results[3].item.tenant_id == "tenant_1"
    assert results[4].item is None
    assert preference.item.memory_id in items.items


async def test_recall_filters_deletion_pending_and_naive_expiry() -> None:
    canonical_items = [
        MemoryItem(
            memory_id="deleting",
            scope="stable_fact",
            subject_id="user_1",
            user_id="user_1",
            tenant_id="tenant_1",
            content="must not leak",
            lifecycle_status="deletion_pending",
        ),
        MemoryItem(
            memory_id="expired",
            scope="stable_fact",
            subject_id="user_1",
            user_id="user_1",
            tenant_id="tenant_1",
            content="expired",
            ttl_expires_at=datetime(2020, 1, 1),
        ),
        MemoryItem(
            memory_id="active",
            scope="stable_fact",
            subject_id="user_1",
            user_id="user_1",
            tenant_id="tenant_1",
            content="active",
        ),
    ]

    class Adapter:
        async def search(self, request):
            del request
            return canonical_items

    repository = MemoryItemRepository()
    for item in canonical_items:
        await repository.add(item)
    service = MemoryService(
        Settings(storage_backend="memory"), repository=repository, adapter=Adapter()
    )
    response = await service.recall(
        MemoryRecallRequest(
            query="active",
            user=UserContext(id="user_1", attributes={"tenant_id": "tenant_1"}),
        )
    )
    assert [item.memory_id for item in response.context.items] == ["active"]
    assert response.expired_count == 1
