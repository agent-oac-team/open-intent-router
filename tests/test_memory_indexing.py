from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.core.config import Settings
from app.db.models import MemoryIndexOperationModel, MemoryItemModel, MemoryRevisionModel
from app.db.session import create_all_tables, create_session_factory
from app.repositories.context_stores import DatabaseMemoryItemRepository, MemoryItemRepository
from app.repositories.execution_traces import MemoryExecutionTraceRepository
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.repositories.memory_index_operations import (
    DatabaseMemoryIndexOutboxRepository,
    MemoryIndexOutboxRepository,
)
from app.repositories.memory_lifecycle_store import (
    DatabaseMemoryLifecycleStore,
    MemoryLifecycleStore,
)
from app.repositories.memory_revisions import MemoryRevisionLedgerRepository
from app.repositories.turns import MemoryTurnRepository
from app.schemas.common import UserContext
from app.schemas.execution_traces import ExecutionTraceQuery
from app.schemas.memory import (
    MemoryDecisionStatus,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTurn,
    MemoryIndexOperation,
    MemoryIndexOperationType,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
    MemoryRecallRequest,
    MemoryRevision,
    MemoryRevisionOperation,
    MemoryWriteCandidate,
)
from app.schemas.turns import CanonicalTurn, TurnUserInput
from app.services.execution_trace_service import ExecutionTraceService
from app.services.memory_adapter import (
    Mem0MemoryAdapter,
    MemoryProviderOperationStatus,
    RepositoryMemoryAdapter,
)
from app.services.memory_indexing import (
    MemoryIndexOperationWorker,
    MemoryIndexRepairService,
)
from app.services.memory_lifecycle import MemoryLifecycleService, MemoryTtlSweeper
from app.services.memory_service import MemoryService
from app.services.turn_service import TurnService


async def test_governed_add_sends_one_canonical_item_with_inference_disabled() -> None:
    repository = MemoryItemRepository()
    client = IndexFakeMem0()
    adapter = _adapter(repository, client)
    item = _item(
        memory_id="mem_1",
        revision_id="mrev_1",
        content="canonical preference",
        structured_value={"messages": [{"role": "user", "content": "full turn"}]},
        metadata={"messages": [{"role": "assistant", "content": "capsule"}]},
    )

    result = await adapter.execute_index_operation(_index_add(item), item=item)

    assert result.status == MemoryProviderOperationStatus.SUCCESS
    assert len(client.add_calls) == 1
    assert client.add_calls[0]["payload"] == "canonical preference"
    assert client.add_calls[0]["infer"] is False
    assert client.add_calls[0]["metadata"]["memory_id"] == "mem_1"
    assert client.add_calls[0]["metadata"]["revision_id"] == "mrev_1"
    assert "messages" not in client.add_calls[0]["metadata"]


async def test_add_adopts_existing_record_removes_duplicates_and_update_is_in_place() -> None:
    repository = MemoryItemRepository()
    client = IndexFakeMem0()
    item = _item(memory_id="mem_1", revision_id="mrev_1", content="old value")
    client.seed(item, external_id="ext_keep")
    client.seed(item, external_id="ext_duplicate")
    adapter = _adapter(repository, client)

    adopted = await adapter.execute_index_operation(_index_add(item), item=item)
    updated_item = item.model_copy(
        update={
            "content": "new value",
            "current_revision_id": "mrev_2",
            "current_revision_no": 2,
            "metadata": {"mem0_memory_id": adopted.external_memory_id},
        }
    )
    updated = await adapter.execute_index_operation(
        MemoryIndexOperation(
            idempotency_key="update:mem_1:mrev_2",
            operation="update",
            memory_id="mem_1",
            revision_id="mrev_2",
            tenant_id="t1",
            external_memory_id=adopted.external_memory_id,
        ),
        item=updated_item,
    )

    assert adopted.adopted is True
    assert adopted.external_memory_id == "ext_duplicate"
    assert adopted.duplicate_external_ids == ("ext_keep",)
    assert client.deleted_ids == ["ext_keep"]
    assert updated.status == MemoryProviderOperationStatus.SUCCESS
    assert updated.external_memory_id == "ext_duplicate"
    assert len(client.records) == 1
    assert client.records["ext_duplicate"]["memory"] == "new value"
    assert client.records["ext_duplicate"]["metadata"]["revision_id"] == "mrev_2"
    assert len(client.add_calls) == 0


async def test_search_uses_only_matching_active_canonical_state_and_content() -> None:
    repository = MemoryItemRepository()
    active = _item(memory_id="mem_active", revision_id="rev_active", content="canonical content")
    deleting = _item(
        memory_id="mem_deleting",
        revision_id="rev_deleting",
        content="must not recall",
    ).model_copy(update={"lifecycle_status": "deletion_pending"})
    expired = _item(
        memory_id="mem_expired",
        revision_id="rev_expired",
        content="expired",
    ).model_copy(update={"ttl_expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    await repository.add(active)
    await repository.add(deleting)
    await repository.add(expired)
    client = IndexFakeMem0()
    client.seed(active, external_id="ext_active", content="forged provider content")
    client.seed(deleting, external_id="ext_deleting")
    client.seed(expired, external_id="ext_expired")
    client.seed(
        _item(memory_id="mem_orphan", revision_id="rev_orphan", content="orphan"),
        external_id="ext_orphan",
    )
    adapter = _adapter(repository, client)

    results = await adapter.search(
        MemoryRecallRequest(
            query="content",
            user=UserContext(id="u1", attributes={"tenant_id": "t1"}),
            scopes=["user_preference"],
            max_items=10,
        )
    )

    assert [item.memory_id for item in results] == ["mem_active"]
    assert results[0].content == "canonical content"
    assert results[0].metadata["mem0_memory_id"] == "ext_active"
    search_event = next(event for event in repository.events if event.event_type == "mem0_search")
    assert search_event.payload["stale_or_orphan_count"] == 3


async def test_delete_not_found_is_idempotent_and_provider_error_is_retryable() -> None:
    repository = MemoryItemRepository()
    client = IndexFakeMem0()
    adapter = _adapter(repository, client)
    operation = MemoryIndexOperation(
        idempotency_key="delete:missing",
        operation="delete",
        memory_id="mem_missing",
        tenant_id="t1",
    )

    missing = await adapter.execute_index_operation(operation, item=None)
    client.fail_delete = True
    retryable = await adapter.delete_provider_record(
        memory_id="mem_missing", external_memory_id="ext_unknown"
    )

    assert missing.status == MemoryProviderOperationStatus.NOT_FOUND
    assert missing.completed is True
    assert retryable.status == MemoryProviderOperationStatus.RETRYABLE_ERROR
    assert retryable.completed is False


async def test_update_provider_failure_is_retryable_without_second_memory() -> None:
    repository = MemoryItemRepository()
    item = _item(memory_id="mem_update", revision_id="rev_2", content="new value").model_copy(
        update={"metadata": {"mem0_memory_id": "ext_update"}}
    )
    client = IndexFakeMem0()
    client.seed(item, external_id="ext_update", content="old value")
    client.fail_update = True
    adapter = _adapter(repository, client)
    operation = MemoryIndexOperation(
        idempotency_key="update:failure",
        operation="update",
        memory_id=item.memory_id,
        revision_id=item.current_revision_id,
        tenant_id="t1",
        external_memory_id="ext_update",
    )

    result = await adapter.execute_index_operation(operation, item=item)

    assert result.status == MemoryProviderOperationStatus.RETRYABLE_ERROR
    assert result.completed is False
    assert list(client.records) == ["ext_update"]
    assert client.records["ext_update"]["memory"] == "old value"


async def test_index_worker_completes_mapping_and_never_completes_provider_failure() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(memory_id="mem_ok", revision_id="rev_ok", content="indexed")
    await repository.add(item)
    await outbox.add(_index_add(item))
    client = IndexFakeMem0()
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-1",
    )

    result = await worker.run_once()

    assert result is not None and result.completed is True
    stored = await repository.get_by_id("mem_ok", tenant_id="t1")
    assert stored is not None
    assert stored.index_status == "ready"
    assert stored.metadata["mem0_memory_id"] == result.provider_result.external_memory_id
    assert result.operation.status == "completed"
    assert any(event.event_type == "memory_index_add" for event in repository.events)

    failed_item = _item(memory_id="mem_fail", revision_id="rev_fail", content="retry")
    await repository.add(failed_item)
    failed_operation = await outbox.add(_index_add(failed_item))
    client.fail_add = True
    failed = await worker.run_once()

    assert failed is not None and failed.completed is False
    assert failed.operation.index_operation_id == failed_operation.index_operation_id
    assert failed.operation.status == "retry"
    assert failed.provider_result.status == MemoryProviderOperationStatus.RETRYABLE_ERROR
    stored_failed = await repository.get_by_id("mem_fail", tenant_id="t1")
    assert stored_failed is not None and stored_failed.index_status == "out_of_sync"


async def test_index_worker_projects_claimed_and_completed_state_for_current_revision() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(memory_id="mem_trace", revision_id="rev_trace", content="indexed").model_copy(
        update={"canonical_refs": ["turn_trace"]}
    )
    await repository.add(item)
    await outbox.add(_index_add(item))
    turn_repository = MemoryTurnRepository()
    now = datetime.now(UTC)
    turn = CanonicalTurn(
        turn_id="turn_trace",
        tenant_id="t1",
        user_id="u1",
        session_id="session_trace",
        request_id="request_trace",
        source="host_chat",
        user_input=TurnUserInput(text="remember this"),
        created_at=now,
        updated_at=now,
    )
    await turn_repository.create_idempotent(turn)
    turns = TurnService(turn_repository)
    traces = ExecutionTraceService(MemoryExecutionTraceRepository(), canonical_turns=turns)
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, IndexFakeMem0()),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-trace",
        execution_traces=traces,
        turns=turns,
    )

    result = await worker.run_once()

    assert result is not None and result.completed is True
    snapshot = await traces.snapshot(
        ExecutionTraceQuery(
            tenant_id="t1",
            user_id="u1",
            session_id="session_trace",
            turn_id="turn_trace",
        )
    )
    revisions = [event for event in snapshot.events if event.event_type == "memory_revision"]
    assert [(event.status, event.facts) for event in revisions] == [
        (
            "claimed",
            {
                "memory_id": "mem_trace",
                "revision_id": "rev_trace",
                "operation": "add",
                "index_status": "pending",
                "index_operation_status": "claimed",
            },
        ),
        (
            "completed",
            {
                "memory_id": "mem_trace",
                "revision_id": "rev_trace",
                "operation": "add",
                "index_status": "ready",
                "index_operation_status": "completed",
            },
        ),
    ]
    assert await worker.run_once() is None
    replay_snapshot = await traces.snapshot(
        ExecutionTraceQuery(
            tenant_id="t1",
            user_id="u1",
            session_id="session_trace",
            turn_id="turn_trace",
        )
    )
    assert [event.event_offset for event in replay_snapshot.events] == [1, 2]


async def test_index_worker_projects_retry_with_out_of_sync_canonical_state() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(
        memory_id="mem_retry_trace", revision_id="rev_retry_trace", content="retry"
    ).model_copy(update={"canonical_refs": ["turn_retry_trace"]})
    await repository.add(item)
    await outbox.add(_index_add(item))
    turn_repository = MemoryTurnRepository()
    now = datetime.now(UTC)
    await turn_repository.create_idempotent(
        CanonicalTurn(
            turn_id="turn_retry_trace",
            tenant_id="t1",
            user_id="u1",
            session_id="session_retry_trace",
            request_id="request_retry_trace",
            source="host_chat",
            user_input=TurnUserInput(text="remember this"),
            created_at=now,
            updated_at=now,
        )
    )
    turns = TurnService(turn_repository)
    traces = ExecutionTraceService(MemoryExecutionTraceRepository(), canonical_turns=turns)
    client = IndexFakeMem0()
    client.fail_add = True
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-retry-trace",
        execution_traces=traces,
        turns=turns,
    )

    result = await worker.run_once()

    assert result is not None and result.operation.status == "retry"
    snapshot = await traces.snapshot(
        ExecutionTraceQuery(
            tenant_id="t1",
            user_id="u1",
            session_id="session_retry_trace",
            turn_id="turn_retry_trace",
        )
    )
    assert snapshot.events[-1].facts == {
        "memory_id": "mem_retry_trace",
        "revision_id": "rev_retry_trace",
        "operation": "add",
        "index_status": "out_of_sync",
        "index_operation_status": "retry",
    }


async def test_index_worker_does_not_project_or_index_a_stale_revision() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    current = _item(
        memory_id="mem_stale_trace",
        revision_id="rev_current",
        content="current value",
    ).model_copy(update={"canonical_refs": ["turn_stale_trace"]})
    await repository.add(current)
    stale_operation = _index_add(current).model_copy(
        update={
            "revision_id": "rev_stale",
            "idempotency_key": "add:mem_stale_trace:rev_stale",
        }
    )
    await outbox.add(stale_operation)
    turn_repository = MemoryTurnRepository()
    now = datetime.now(UTC)
    await turn_repository.create_idempotent(
        CanonicalTurn(
            turn_id="turn_stale_trace",
            tenant_id="t1",
            user_id="u1",
            session_id="session_stale_trace",
            request_id="request_stale_trace",
            source="host_chat",
            user_input=TurnUserInput(text="remember current value"),
            created_at=now,
            updated_at=now,
        )
    )
    turns = TurnService(turn_repository)
    traces = ExecutionTraceService(MemoryExecutionTraceRepository(), canonical_turns=turns)
    client = IndexFakeMem0()
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-stale-trace",
        execution_traces=traces,
        turns=turns,
    )

    result = await worker.run_once()

    assert result is not None
    assert result.provider_result.status == MemoryProviderOperationStatus.SUPERSEDED
    assert client.add_calls == []
    stored = await repository.get_by_id(current.memory_id, tenant_id="t1")
    assert stored is not None and stored.index_status == "pending"
    snapshot = await traces.snapshot(
        ExecutionTraceQuery(
            tenant_id="t1",
            user_id="u1",
            session_id="session_stale_trace",
            turn_id="turn_stale_trace",
        )
    )
    assert snapshot.events == []


async def test_index_worker_projects_dead_letter_terminal_state() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(
        memory_id="mem_dead_trace",
        revision_id="rev_dead_trace",
        content="dead letter",
    ).model_copy(update={"canonical_refs": ["turn_dead_trace"]})
    await repository.add(item)
    await outbox.add(_index_add(item).model_copy(update={"max_attempts": 1}))
    turn_repository = MemoryTurnRepository()
    now = datetime.now(UTC)
    await turn_repository.create_idempotent(
        CanonicalTurn(
            turn_id="turn_dead_trace",
            tenant_id="t1",
            user_id="u1",
            session_id="session_dead_trace",
            request_id="request_dead_trace",
            source="host_chat",
            user_input=TurnUserInput(text="remember this"),
            created_at=now,
            updated_at=now,
        )
    )
    turns = TurnService(turn_repository)
    traces = ExecutionTraceService(MemoryExecutionTraceRepository(), canonical_turns=turns)
    client = IndexFakeMem0()
    client.fail_add = True
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-dead-trace",
        execution_traces=traces,
        turns=turns,
    )

    result = await worker.run_once()

    assert result is not None and result.operation.status == "dead_letter"
    snapshot = await traces.snapshot(
        ExecutionTraceQuery(
            tenant_id="t1",
            user_id="u1",
            session_id="session_dead_trace",
            turn_id="turn_dead_trace",
        )
    )
    assert snapshot.events[-1].facts["index_status"] == "dead_letter"
    assert snapshot.events[-1].facts["index_operation_status"] == "dead_letter"


async def test_index_worker_does_not_write_non_active_canonical_item() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(memory_id="mem_deleting", revision_id="rev_deleting", content="private")
    await repository.add(item.model_copy(update={"lifecycle_status": "deletion_pending"}))
    await outbox.add(_index_add(item))
    client = IndexFakeMem0()
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-superseded",
    )

    result = await worker.run_once()

    assert result is not None and result.completed is True
    assert result.provider_result.status == MemoryProviderOperationStatus.SUPERSEDED
    assert client.add_calls == []
    assert result.operation.status == "completed"
    event = next(event for event in repository.events if event.event_type == "memory_index_add")
    assert event.payload["provider_status"] == "superseded"


async def test_delete_worker_hard_deletes_after_provider_success() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    revisions = store.revision_repository
    item = _item(
        memory_id="mem_delete",
        revision_id="rev_delete",
        content="remove me",
        metadata={"mem0_memory_id": "ext_delete"},
    )
    await repository.add(item)
    revisions.revisions[item.memory_id] = [
        MemoryRevision(
            revision_id="rev_delete",
            memory_id=item.memory_id,
            revision_no=1,
            memory_key=item.memory_key or "key",
            operation=MemoryRevisionOperation.ADD,
            content=item.content,
            policy_version="policy-v1",
        )
    ]
    lifecycle = MemoryLifecycleService(settings=settings, store=store)
    requested = await lifecycle.request_delete(_delete_operation(item))
    assert requested.index_operation is not None
    client = IndexFakeMem0()
    client.seed(item, external_id="ext_delete")
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-delete",
    )

    result = await worker.run_once()

    assert result is not None and result.completed is True
    assert await repository.get_by_id("mem_delete", tenant_id="t1") is None
    assert revisions.revisions.get("mem_delete") is None
    tombstone = next(
        event for event in repository.events if event.event_type == "memory_deleted_tombstone"
    )
    assert "remove me" not in str(tombstone.model_dump())
    assert client.deleted_ids == ["ext_delete"]


async def test_ttl_worker_hard_deletes_revision_provider_and_pending_payloads() -> None:
    settings = _settings()
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    revisions = MemoryRevisionLedgerRepository(repository)
    formation = MemoryFormationTurnJobRepository()
    store = MemoryLifecycleStore(
        item_repository=repository,
        revision_repository=revisions,
        index_repository=outbox,
        formation_repository=formation,
    )
    item = _item(
        memory_id="mem_ttl_delete",
        revision_id="rev_ttl_delete",
        content="ttl private content",
        metadata={"mem0_memory_id": "ext_ttl_delete"},
    ).model_copy(
        update={
            "scope": "task_memory",
            "ttl_expires_at": datetime(2020, 1, 1, tzinfo=UTC),
        }
    )
    await repository.add(item)
    revisions.revisions[item.memory_id] = [
        MemoryRevision(
            revision_id=item.current_revision_id or "",
            memory_id=item.memory_id,
            revision_no=1,
            memory_key=item.memory_key or "key",
            operation=MemoryRevisionOperation.ADD,
            content=item.content,
            policy_version="policy-v1",
        )
    ]
    turn = MemoryFormationTurn(
        turn_id="turn_ttl_delete",
        request_id="request_ttl_delete",
        session_id="session_ttl_delete",
        user_id="u1",
        tenant_id="t1",
        user_text="ttl source content",
        assistant_text="ttl assistant content",
        result_status="completed",
        used_memory_ids=[item.memory_id],
    )
    await formation.append_turn(turn)
    job = MemoryFormationJob(
        job_id="job_ttl_delete",
        trigger="manual",
        mode="enforced",
        tenant_id="t1",
        user_id="u1",
        session_id=turn.session_id,
        source_refs=[turn.turn_id],
        idempotency_key="job:ttl-delete",
        model_version="v1",
        prompt_version="v1",
        policy_version="v1",
        trace_summary={"content": "ttl source content", "memory_id": item.memory_id},
    )
    formation.jobs[job.job_id] = job
    formation.jobs_by_idempotency[job.idempotency_key] = job.job_id
    lifecycle = MemoryLifecycleService(settings=settings, store=store)

    pending = await MemoryTtlSweeper(
        lifecycle=lifecycle,
        store=store,
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    ).run_once()

    assert len(pending) == 1
    assert pending[0].operation.reason_code == MemoryFormationReasonCode.TTL_EXPIRED
    assert formation.turns[turn.turn_id].user_text == ""
    assert formation.turns[turn.turn_id].assistant_text == ""
    assert formation.turns[turn.turn_id].used_memory_ids == []
    assert formation.jobs[job.job_id].trace_summary["content"] == "[redacted]"
    client = IndexFakeMem0()
    client.seed(item, external_id="ext_ttl_delete")
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-ttl-delete",
    )

    completed = await worker.run_once()

    assert completed is not None and completed.completed is True
    assert await repository.get_by_id(item.memory_id, tenant_id="t1") is None
    assert revisions.revisions.get(item.memory_id) is None
    assert client.records == {}
    assert client.deleted_ids == ["ext_ttl_delete"]


async def test_delete_scan_failure_keeps_canonical_and_all_provider_records() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(
        memory_id="mem_delete_scan",
        revision_id="rev_delete_scan",
        content="sensitive value",
        metadata={"mem0_memory_id": "ext_mapped"},
    )
    await repository.add(item)
    lifecycle = MemoryLifecycleService(settings=settings, store=store)
    await lifecycle.request_delete(_delete_operation(item))
    client = IndexFakeMem0()
    client.seed(item, external_id="ext_mapped")
    client.seed(item, external_id="ext_duplicate")
    client.fail_scan = True
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-delete-scan-failure",
    )

    result = await worker.run_once()

    assert result is not None and result.completed is False
    assert result.provider_result.status == MemoryProviderOperationStatus.RETRYABLE_ERROR
    stored = await repository.get_by_id(item.memory_id, tenant_id="t1")
    assert stored is not None and stored.lifecycle_status == "deletion_pending"
    assert set(client.records) == {"ext_mapped", "ext_duplicate"}
    assert client.deleted_ids == []


async def test_delete_not_found_is_preserved_in_tombstone_event() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(
        memory_id="mem_delete_missing",
        revision_id="rev_delete_missing",
        content="remove missing",
    )
    await repository.add(item)
    lifecycle = MemoryLifecycleService(settings=settings, store=store)
    await lifecycle.request_delete(_delete_operation(item))
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=_adapter(repository, IndexFakeMem0()),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="worker-delete-not-found",
    )

    result = await worker.run_once()

    assert result is not None and result.completed is True
    assert result.provider_result.status == MemoryProviderOperationStatus.NOT_FOUND
    tombstone = next(
        event for event in repository.events if event.event_type == "memory_deleted_tombstone"
    )
    assert tombstone.payload["provider_status"] == "not_found"


async def test_delete_provider_status_survives_crash_before_tombstone() -> None:
    settings = _settings()
    repository, outbox, store = _stores()
    item = _item(
        memory_id="mem_delete_crash",
        revision_id="rev_delete_crash",
        content="remove after crash",
    )
    await repository.add(item)
    lifecycle = MemoryLifecycleService(settings=settings, store=store)
    await lifecycle.request_delete(_delete_operation(item))
    now = datetime.now(UTC)
    claimed = await outbox.claim(owner="crashed-worker", now=now, lease_seconds=30)
    assert claimed is not None
    completed = await outbox.complete(
        claimed.index_operation_id,
        owner="crashed-worker",
        lease_token=claimed.lease_token,
        now=now + timedelta(seconds=1),
        result_metadata={"provider_status": "not_found"},
    )
    assert completed.last_error_metadata["provider_status"] == "not_found"
    repair = MemoryIndexRepairService(
        adapter=_adapter(repository, IndexFakeMem0()),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )

    result = await repair.run_tenant(tenant_id="t1")

    assert result.delete_finalized_count == 1
    tombstone = next(
        event for event in repository.events if event.event_type == "memory_deleted_tombstone"
    )
    assert tombstone.payload["provider_status"] == "not_found"


async def test_repair_rebuilds_missing_and_cleans_duplicate_and_orphan_records() -> None:
    repository, outbox, store = _stores()
    missing = _item(memory_id="mem_missing", revision_id="rev_missing", content="missing")
    duplicate = _item(
        memory_id="mem_duplicate", revision_id="rev_duplicate", content="canonical duplicate"
    )
    await repository.add(missing)
    await repository.add(duplicate)
    client = IndexFakeMem0()
    client.seed(duplicate, external_id="ext_duplicate_a", content="stale")
    client.seed(duplicate, external_id="ext_duplicate_b", content="stale")
    orphan = _item(memory_id="mem_orphan", revision_id="rev_orphan", content="orphan")
    client.seed(orphan, external_id="ext_orphan")
    adapter = _adapter(repository, client)
    repair = MemoryIndexRepairService(
        adapter=adapter,
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )

    result = await repair.run_tenant(tenant_id="t1")

    assert result.status == "ok"
    assert result.added_count == 1
    assert result.updated_count == 1
    assert result.duplicate_deleted_count == 1
    assert result.orphan_deleted_count == 1
    assert {record["metadata"]["memory_id"] for record in client.records.values()} == {
        "mem_missing",
        "mem_duplicate",
    }
    assert all(
        record["memory"] in {"missing", "canonical duplicate"} for record in client.records.values()
    )

    rebuilt = await repair.run_tenant(tenant_id="t1", rebuild=True)
    assert rebuilt.status == "ok"
    assert rebuilt.added_count == 2
    assert len(client.records) == 2


async def test_repair_paginates_complete_canonical_and_provider_views() -> None:
    repository, outbox, store = _stores()
    client = IndexFakeMem0()
    for index in range(3):
        item = _item(
            memory_id=f"mem_page_{index}",
            revision_id=f"rev_page_{index}",
            content=f"page {index}",
        )
        await repository.add(item)
        client.seed(item, external_id=f"ext_page_{index}")
    orphan = _item(memory_id="mem_orphan", revision_id="rev_orphan", content="orphan")
    client.seed(orphan, external_id="ext_orphan")
    repair = MemoryIndexRepairService(
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )

    result = await repair.run_tenant(tenant_id="t1", limit=2)

    assert result.status == "ok"
    assert result.canonical_count == 3
    assert result.orphan_deleted_count == 1
    assert set(client.records) == {"ext_page_0", "ext_page_1", "ext_page_2"}


async def test_repair_keyset_pagination_survives_concurrent_updated_at_change() -> None:
    repository = MovingUpdateMemoryRepository()
    outbox = MemoryIndexOutboxRepository()
    revisions = MemoryRevisionLedgerRepository(repository)
    store = MemoryLifecycleStore(
        item_repository=repository,
        revision_repository=revisions,
        index_repository=outbox,
    )
    client = IndexFakeMem0()
    for index in range(3):
        item = _item(
            memory_id=f"mem_{index}",
            revision_id=f"rev_{index}",
            content=f"value {index}",
        )
        await repository.add(item)
        client.seed(item, external_id=f"ext_{index}")
    repair = MemoryIndexRepairService(
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )

    result = await repair.run_tenant(tenant_id="t1", limit=2)

    assert result.status == "ok"
    assert result.canonical_count == 3
    assert result.orphan_deleted_count == 0
    assert set(client.records) == {"ext_0", "ext_1", "ext_2"}


async def test_repair_scan_failure_is_degraded_and_does_not_mutate_provider() -> None:
    repository, outbox, store = _stores()
    item = _item(memory_id="mem_repair", revision_id="rev_repair", content="canonical")
    await repository.add(item)
    client = IndexFakeMem0()
    client.seed(item, external_id="ext_repair")
    client.fail_scan = True
    repair = MemoryIndexRepairService(
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )

    result = await repair.run_tenant(tenant_id="t1")

    assert result.status == "degraded"
    assert result.failed_count == 1
    assert set(client.records) == {"ext_repair"}
    assert client.deleted_ids == []


async def test_repair_compensates_provider_add_when_canonical_cas_is_lost() -> None:
    repository = LosingCasMemoryRepository()
    outbox = MemoryIndexOutboxRepository()
    revisions = MemoryRevisionLedgerRepository(repository)
    store = MemoryLifecycleStore(
        item_repository=repository,
        revision_repository=revisions,
        index_repository=outbox,
    )
    item = _item(memory_id="mem_lost", revision_id="rev_lost", content="must stay deleted")
    await repository.add(item)
    client = IndexFakeMem0()
    repair = MemoryIndexRepairService(
        adapter=_adapter(repository, client),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )

    result = await repair.run_tenant(tenant_id="t1")

    assert result.status == "error"
    assert result.failed_count == 1
    assert repository.items == {}
    assert client.records == {}
    assert len(client.add_calls) == 1
    assert len(client.deleted_ids) == 1


async def test_failed_repair_compensation_is_durable_and_recovers_after_restart() -> None:
    repository = LosingCasMemoryRepository()
    outbox = MemoryIndexOutboxRepository()
    revisions = MemoryRevisionLedgerRepository(repository)
    store = MemoryLifecycleStore(
        item_repository=repository,
        revision_repository=revisions,
        index_repository=outbox,
    )
    item = _item(
        memory_id="mem_compensation_retry",
        revision_id="rev_compensation_retry",
        content="must stay deleted",
    )
    await repository.add(item)
    client = IndexFakeMem0()
    client.fail_delete = True
    adapter = _adapter(repository, client)
    repair = MemoryIndexRepairService(
        adapter=adapter,
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )

    failed = await repair.run_tenant(tenant_id="t1")

    assert failed.status == "error"
    assert failed.error_code == "compensation_delete_retry"
    assert failed.compensation_retry_count == 1
    assert repository.items == {}
    assert len(client.records) == 1
    pending = list(outbox.operations.values())
    assert len(pending) == 1
    assert pending[0].operation == "delete"
    assert pending[0].external_memory_id in client.records
    assert pending[0].last_error_metadata["repair_compensation"] is True

    client.fail_delete = False
    restarted_worker = MemoryIndexOperationWorker(
        settings=_settings(),
        adapter=adapter,
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="restarted-compensation-worker",
    )
    recovered = await restarted_worker.run_once()

    assert recovered is not None and recovered.completed is True
    assert recovered.operation.status == "completed"
    assert client.records == {}
    assert any(
        event.event_type == "memory_index_delete" and event.payload["provider_status"] == "success"
        for event in repository.events
    )


async def test_database_failed_compensation_recovers_after_process_restart(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'compensation-restart.db'}",
        memory_strategy_provider="mem0",
        memory_formation_model_timeout_seconds=1,
        memory_formation_lease_seconds=10,
    )
    await create_all_tables(settings)
    first_factory = create_session_factory(settings)
    repository = LosingCasDatabaseRepository(first_factory)
    outbox = DatabaseMemoryIndexOutboxRepository(first_factory)
    store = DatabaseMemoryLifecycleStore(first_factory)
    item = _item(
        memory_id="mem_db_compensation",
        revision_id="rev_db_compensation",
        content="database sensitive value",
    )
    await repository.add(item)
    client = IndexFakeMem0()
    client.fail_delete = True
    repair = MemoryIndexRepairService(
        adapter=Mem0MemoryAdapter(
            settings,
            repository,
            client_factory=lambda _config: client,
        ),
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    )
    failed = await repair.run_tenant(tenant_id="t1")
    assert failed.compensation_retry_count == 1
    await first_factory.kw["bind"].dispose()

    second_factory = create_session_factory(settings)
    restarted_repository = DatabaseMemoryItemRepository(second_factory)
    restarted_outbox = DatabaseMemoryIndexOutboxRepository(second_factory)
    restarted_store = DatabaseMemoryLifecycleStore(second_factory)
    client.fail_delete = False
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=Mem0MemoryAdapter(
            settings,
            restarted_repository,
            client_factory=lambda _config: client,
        ),
        repository=restarted_repository,
        outbox=restarted_outbox,
        lifecycle_store=restarted_store,
        owner="database-restarted-worker",
    )
    try:
        recovered = await worker.run_once()
        assert recovered is not None and recovered.completed is True
        assert recovered.operation.status == "completed"
        assert recovered.operation.last_error_metadata["repair_compensation"] is True
        assert client.records == {}
    finally:
        await second_factory.kw["bind"].dispose()


async def test_repository_fallback_reports_degraded_for_index_and_repair() -> None:
    repository, outbox, store = _stores()
    item = _item(memory_id="mem_1", revision_id="rev_1", content="canonical")
    await repository.add(item)
    adapter = RepositoryMemoryAdapter(repository)

    provider = await adapter.execute_index_operation(_index_add(item), item=item)
    repair = await MemoryIndexRepairService(
        adapter=adapter,
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
    ).run_tenant(tenant_id="t1")

    assert provider.status == MemoryProviderOperationStatus.DEGRADED
    assert provider.completed is False
    assert repair.status == "degraded"
    assert repair.failed_count == 1
    assert repair.error_code == "repository_fallback"


async def test_database_worker_persists_mapping_and_completes_hard_delete(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'index-worker.db'}",
        memory_strategy_provider="mem0",
        memory_formation_model_timeout_seconds=1,
        memory_formation_lease_seconds=10,
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    repository = DatabaseMemoryItemRepository(session_factory)
    outbox = DatabaseMemoryIndexOutboxRepository(session_factory)
    store = DatabaseMemoryLifecycleStore(session_factory)
    client = IndexFakeMem0()
    adapter = Mem0MemoryAdapter(settings, repository, client_factory=lambda _config: client)
    worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=adapter,
        repository=repository,
        outbox=outbox,
        lifecycle_store=store,
        owner="database-worker",
    )
    try:
        item = _item(memory_id="mem_database", revision_id="rev_database", content="db value")
        await repository.add(item)
        await outbox.add(_index_add(item))

        indexed = await worker.run_once()
        stored = await repository.get_by_id("mem_database", tenant_id="t1")

        assert indexed is not None and indexed.completed is True
        assert stored is not None and stored.index_status == "ready"
        assert stored.metadata["mem0_memory_id"] == indexed.provider_result.external_memory_id

        lifecycle = MemoryLifecycleService(settings=settings, store=store)
        await lifecycle.request_delete(_delete_operation(stored))
        deleted = await worker.run_once()

        assert deleted is not None and deleted.completed is True
        assert await repository.get_by_id("mem_database", tenant_id="t1") is None
        events = await repository.list_events(tenant_id="t1", limit=20)
        tombstone = next(
            event for event in events if event.event_type == "memory_deleted_tombstone"
        )
        assert tombstone.payload["provider_status"] == "success"
    finally:
        await session_factory.kw["bind"].dispose()


async def test_database_explicit_write_commits_revision_and_outbox_before_provider(
    tmp_path,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'explicit-canonical.db'}",
        memory_strategy_provider="mem0",
        memory_formation_model_timeout_seconds=1,
        memory_formation_lease_seconds=10,
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    repository = DatabaseMemoryItemRepository(session_factory)
    client = IndexFakeMem0()
    service = MemoryService(
        settings=settings,
        repository=repository,
        adapter=Mem0MemoryAdapter(
            settings,
            repository,
            client_factory=lambda _config: client,
        ),
    )
    try:
        decisions = await service.write_candidates(
            candidates=[
                MemoryWriteCandidate(
                    scope="stable_fact",
                    content="canonical before provider",
                    confidence=0.95,
                )
            ],
            user_id="u1",
            tenant_id="t1",
        )

        assert decisions[0].status == "accepted"
        assert decisions[0].metadata["mem0_status"] == "success"
        async with session_factory() as session:
            revision = await session.get(MemoryRevisionModel, decisions[0].metadata["revision_id"])
            operation = await session.scalar(
                select(MemoryIndexOperationModel).where(
                    MemoryIndexOperationModel.memory_id == decisions[0].memory_id
                )
            )
        assert revision is not None
        assert revision.memory_id == decisions[0].memory_id
        assert operation is not None and operation.status == "completed"
        assert operation.external_memory_id == decisions[0].metadata["mem0_memory_id"]
        assert len(client.add_calls) == 1
    finally:
        await session_factory.kw["bind"].dispose()


class IndexFakeMem0:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.add_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.deleted_ids: list[str] = []
        self.fail_add = False
        self.fail_update = False
        self.fail_delete = False
        self.fail_scan = False
        self._next_id = 1

    def seed(self, item: MemoryItem, *, external_id: str, content: str | None = None) -> None:
        self.records[external_id] = {
            "memory": content if content is not None else item.content,
            "metadata": {
                "memory_id": item.memory_id,
                "revision_id": item.current_revision_id,
                "tenant_id": item.tenant_id,
                "user_id": item.user_id,
                "subject_type": item.subject_type,
                "subject_id": item.subject_id,
                "scope": str(item.scope),
            },
        }

    def add(self, payload, *, user_id: str, metadata: dict, infer: bool):
        if self.fail_add:
            raise RuntimeError("provider add unavailable")
        external_id = f"ext_{self._next_id}"
        self._next_id += 1
        self.add_calls.append(
            {
                "payload": payload,
                "user_id": user_id,
                "metadata": metadata,
                "infer": infer,
            }
        )
        self.records[external_id] = {"memory": payload, "metadata": dict(metadata)}
        return {"results": [{"id": external_id, "memory": payload}]}

    def update(self, *, memory_id: str, data: str, metadata: dict):
        if self.fail_update:
            raise RuntimeError("provider update unavailable")
        if memory_id not in self.records:
            raise RuntimeError("Memory not found")
        self.update_calls.append({"memory_id": memory_id, "data": data, "metadata": metadata})
        self.records[memory_id] = {"memory": data, "metadata": dict(metadata)}
        return {"message": "updated"}

    def delete(self, *, memory_id: str):
        if self.fail_delete:
            raise RuntimeError("provider delete unavailable")
        if memory_id not in self.records:
            raise RuntimeError("Memory not found")
        self.deleted_ids.append(memory_id)
        self.records.pop(memory_id)

    def get_all(self, *, filters: dict, top_k: int):
        if self.fail_scan:
            raise RuntimeError("provider scan unavailable")
        return {
            "results": [
                {"id": external_id, **record}
                for external_id, record in self.records.items()
                if all(record["metadata"].get(key) == value for key, value in filters.items())
            ][:top_k]
        }

    def search(self, query: str, *, filters: dict, top_k: int):
        return self.get_all(filters=filters, top_k=top_k)


class LosingCasMemoryRepository(MemoryItemRepository):
    async def set_index_state(self, **kwargs):
        self.items.pop(kwargs["memory_id"], None)
        return None


class MovingUpdateMemoryRepository(MemoryItemRepository):
    def __init__(self) -> None:
        super().__init__()
        self.page_calls = 0

    async def list_indexable(self, **kwargs):
        page = await super().list_indexable(**kwargs)
        self.page_calls += 1
        if self.page_calls == 1 and page:
            first = self.items[page[0].memory_id]
            self.items[first.memory_id] = first.model_copy(
                update={"updated_at": first.updated_at + timedelta(days=30)}
            )
        return page


class LosingCasDatabaseRepository(DatabaseMemoryItemRepository):
    async def set_index_state(self, **kwargs):
        async with self.session_factory() as session:
            await session.execute(
                delete(MemoryItemModel).where(MemoryItemModel.memory_id == kwargs["memory_id"])
            )
            await session.commit()
        return None


def _settings() -> Settings:
    return Settings(
        storage_backend="memory",
        memory_strategy_provider="mem0",
        memory_formation_model_timeout_seconds=1,
        memory_formation_lease_seconds=10,
        memory_formation_retry_base_seconds=1,
        memory_formation_retry_max_seconds=4,
    )


def _adapter(repository: MemoryItemRepository, client: IndexFakeMem0) -> Mem0MemoryAdapter:
    return Mem0MemoryAdapter(_settings(), repository, client_factory=lambda _config: client)


def _stores():
    repository = MemoryItemRepository()
    outbox = MemoryIndexOutboxRepository()
    revisions = MemoryRevisionLedgerRepository(repository)
    store = MemoryLifecycleStore(
        item_repository=repository,
        revision_repository=revisions,
        index_repository=outbox,
    )
    return repository, outbox, store


def _item(
    *,
    memory_id: str,
    revision_id: str,
    content: str,
    structured_value: dict | None = None,
    metadata: dict | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        memory_key=f"tenant:t1:user:u1:preference:{memory_id}",
        scope="user_preference",
        subject_id="u1",
        user_id="u1",
        tenant_id="t1",
        content=content,
        structured_value=structured_value or {},
        metadata=metadata or {},
        current_revision_id=revision_id,
        current_revision_no=1,
        index_status="pending",
    )


def _index_add(item: MemoryItem) -> MemoryIndexOperation:
    return MemoryIndexOperation(
        idempotency_key=f"add:{item.memory_id}:{item.current_revision_id}",
        operation=MemoryIndexOperationType.ADD,
        memory_id=item.memory_id,
        revision_id=item.current_revision_id,
        tenant_id=item.tenant_id or "",
    )


def _delete_operation(item: MemoryItem) -> MemoryLifecycleOperation:
    return MemoryLifecycleOperation(
        operation_id=f"delete:{item.memory_id}",
        operation=MemoryOperation.DELETE,
        decision_status=MemoryDecisionStatus.ACCEPTED,
        reason_code=MemoryFormationReasonCode.AUTHORIZED_DELETE,
        tenant_id=item.tenant_id or "",
        user_id=item.user_id or "",
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        memory_key=item.memory_key or "",
        candidate_hash=f"sha256:{'1' * 64}",
        memory_id=item.memory_id,
        revision_id=item.current_revision_id,
    )
