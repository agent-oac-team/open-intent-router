import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.security import memory_identity_signature
from app.db.session import create_all_tables, create_session_factory
from app.dependencies import (
    get_memory_management_service,
    get_memory_observability_service,
    get_memory_service,
)
from app.main import create_app
from app.repositories.context_stores import DatabaseMemoryItemRepository, MemoryItemRepository
from app.repositories.memory_formation import (
    DatabaseMemoryFormationTurnJobRepository,
    MemoryFormationTurnJobRepository,
)
from app.repositories.memory_traces import (
    DatabaseMemoryFormationTraceRepository,
    MemoryFormationTraceRepository,
)
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import MemoryTurnRepository
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryDecisionStatus,
    MemoryEvent,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTurn,
    MemoryIndexOperation,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
)
from app.schemas.turns import (
    CanonicalTurn,
    TurnOutboxEvent,
    TurnSemanticResponse,
    TurnStatus,
    TurnUserInput,
)
from app.services.memory_adapter import (
    MemoryIndexOperationResult,
    MemoryProviderOperationStatus,
)
from app.services.memory_candidate_policy import CandidatePolicyResult
from app.services.memory_management import (
    MemoryManagementConflict,
    MemoryManagementNotFound,
    MemoryManagementService,
)
from app.services.memory_observability import MemoryObservabilityService, _request_trace_stage
from app.services.memory_service import MemoryService

_MEMORY_IDENTITY_SECRET = "memory-identity-secret"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("outbox_pending", ("outbox_pending", False, True, None)),
        ("formation_pending", ("formation_pending", False, True, None)),
        ("formation_retry", ("formation_retry", False, True, "provider_timeout")),
        ("formation_dead_letter", ("formation_dead_letter", True, False, "invalid_json")),
        ("skipped", ("formation_skipped", True, False, "formation_mode_off")),
        ("index_pending", ("memory_persisted_index_pending", False, True, None)),
        ("persisted", ("persisted", True, False, None)),
        ("trace_missing", ("trace_missing", False, True, "turn_outbox_missing")),
    ],
)
def test_request_trace_stage_matrix(case: str, expected: tuple) -> None:
    outboxes = []
    formation_turns = []
    traces = []
    items = []
    events = []
    index_operations = []

    if case == "outbox_pending":
        outboxes = [SimpleNamespace(status="pending", last_error_code=None)]
    elif case == "formation_pending":
        formation_turns = [SimpleNamespace(status="pending")]
    elif case in {"formation_retry", "formation_dead_letter"}:
        formation_turns = [SimpleNamespace(status="claimed")]
        traces = [
            SimpleNamespace(
                job=SimpleNamespace(
                    status="retry" if case == "formation_retry" else "dead_letter",
                    last_error_code=(
                        "provider_timeout" if case == "formation_retry" else "invalid_json"
                    ),
                ),
                candidate_count=0,
                decisions=[],
            )
        ]
    elif case == "skipped":
        events = [
            SimpleNamespace(
                event_type="formation_skipped",
                payload={"reason_code": "formation_mode_off"},
            )
        ]
    elif case in {"index_pending", "persisted"}:
        formation_turns = [SimpleNamespace(status="completed")]
        traces = [
            SimpleNamespace(
                job=SimpleNamespace(status="completed", last_error_code=None),
                candidate_count=1,
                decisions=[SimpleNamespace(decision_status="accepted")],
            )
        ]
        items = [SimpleNamespace(index_status="pending" if case == "index_pending" else "ready")]

    actual = _request_trace_stage(
        turn_status="completed",
        outboxes=outboxes,
        formation_turns=formation_turns,
        traces=traces,
        items=items,
        index_operations=index_operations,
        events=events,
    )

    assert actual == expected


def _memory_actor_headers(user_id: str, tenant_id: str) -> dict[str, str]:
    return {
        "X-User-ID": user_id,
        "X-Tenant-ID": tenant_id,
        "X-Memory-Identity-Signature": memory_identity_signature(
            user_id=user_id,
            tenant_id=tenant_id,
            secret=_MEMORY_IDENTITY_SECRET,
        ),
    }


def _candidate(
    *,
    operation: str = "add",
    content: str = "Use concise answers",
    confidence: float = 0.95,
) -> MemoryFormationCandidate:
    return MemoryFormationCandidate(
        candidate_id=f"candidate_{operation}_{confidence}",
        proposed_operation=operation,
        scope="user_preference",
        content=content,
        structured_value={"slot": "response_style", "value": content},
        subject_id_hint="u1",
        tenant_id_hint="t1",
        memory_key_hint="tenant:t1:user:u1:preference:response_style",
        confidence=confidence,
        evidence_refs=[
            {
                "turn_id": "turn_1",
                "role": "user",
                "quote": content,
                "content_hash": "sha256:evidence",
            }
        ],
        reason="user preference",
    )


def _operation(
    *,
    operation: MemoryOperation,
    status: MemoryDecisionStatus,
    reason: MemoryFormationReasonCode,
    memory_id: str | None = None,
    revision_id: str | None = None,
    job_id: str = "job_1",
    suffix: str = "1",
) -> MemoryLifecycleOperation:
    return MemoryLifecycleOperation(
        operation_id=f"operation_{suffix}",
        operation=operation,
        decision_status=status,
        reason_code=reason,
        tenant_id="t1",
        user_id="u1",
        subject_type="user",
        subject_id="u1",
        memory_key="tenant:t1:user:u1:preference:response_style",
        candidate_hash=f"sha256:candidate-{suffix}",
        memory_id=memory_id,
        revision_id=revision_id,
        formation_job_id=job_id,
        canonical_refs=["turn:turn_1"],
    )


def _services(settings: Settings | None = None):
    settings = settings or Settings(storage_backend="memory", memory_formation_mode="observe")
    items = MemoryItemRepository()
    memory = MemoryService(settings=settings, repository=items)
    formation = MemoryFormationTurnJobRepository()
    traces = MemoryFormationTraceRepository(
        formation_repository=formation,
        event_repository=items,
    )
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory,
        formation_repository=formation,
        trace_repository=traces,
    )
    management = MemoryManagementService(memory_service=memory)
    return items, memory, formation, observability, management


class _CompletingIndexAdapter:
    async def execute_index_operation(self, operation, *, item):
        del item
        return MemoryIndexOperationResult(
            operation=operation.operation,
            status=(
                MemoryProviderOperationStatus.NOT_FOUND
                if operation.operation == "delete"
                else MemoryProviderOperationStatus.SUCCESS
            ),
            memory_id=operation.memory_id,
            external_memory_id=(None if operation.operation == "delete" else "ext_test"),
        )


async def _add_current(memory: MemoryService, *, job_id: str = "job_1"):
    result = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(),
            operation=_operation(
                operation=MemoryOperation.ADD,
                status=MemoryDecisionStatus.ACCEPTED,
                reason=MemoryFormationReasonCode.ACCEPTED_NEW,
                job_id=job_id,
            ),
            redacted_trace={},
        )
    )
    assert result.item and result.revision and result.index_operation
    return result


async def _add_pending_delete(memory: MemoryService, added, *, job_id: str = "job_1"):
    result = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(
                operation="delete", content="Delete this preference", confidence=0.8
            ),
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.AMBIGUOUS_DELETE,
                memory_id=added.item.memory_id,
                revision_id=added.revision.revision_id,
                job_id=job_id,
                suffix=f"pending-delete-{job_id}",
            ),
            redacted_trace={},
        )
    )
    assert result.event
    return result


async def test_debug_filters_assemble_bounded_separate_formation_trace() -> None:
    items, memory, formation, observability, _ = _services()
    now = datetime(2026, 7, 13, tzinfo=UTC)
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
            completed_at=now,
        )
    )
    job = await formation.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="session_1",
        trigger="idle",
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    formation.jobs[job.job_id] = job.model_copy(
        deep=True,
        update={
            "trace_summary": {
                "semantic_contract_version": "v1",
                "semantic_validation_counts": {"confirmed": 1, "private-value": 99},
                "semantic_verifier_counts": {"not_configured": 1, "private-value": 99},
            }
        },
    )
    added = await _add_current(memory, job_id=job.job_id)
    await items.add_event(
        added.event.model_copy(
            update={
                "event_id": "linked_event",
                "request_id": "request_1",
                "session_id": "session_1",
                "turn_id": "turn_1",
                "run_id": "run_1",
                "agent_id": "agent_1",
            }
        )
    )
    assert await memory.record_recall_usage(
        [added.item.to_context_item(relevance=0.91)],
        user_id="u1",
        tenant_id="t1",
        agent_id="agent_1",
        consumer="agent:agent_1",
        request_id="request_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
    )
    for _ in range(2):
        assert await memory.record_recall_usage(
            [added.item.to_context_item(relevance=0.82)],
            user_id="u1",
            tenant_id="t1",
            consumer="router",
            request_id="request_1",
            session_id="session_1",
        )

    response = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        agent_id="agent_1",
        scopes=["user_preference"],
        memory_id=added.item.memory_id,
        request_id="request_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
        formation_job_id=job.job_id,
        memory_key=added.item.memory_key,
        decision_status="accepted",
    )

    assert [item.memory_id for item in response.items] == [added.item.memory_id]
    assert [revision.revision_id for revision in response.revisions] == [added.revision.revision_id]
    assert response.revisions[0].content_preview == "Use concise answers"
    assert len(response.formation_traces) == 1
    trace = response.formation_traces[0]
    assert trace.job.job_id == job.job_id
    assert trace.links.request_ids == ["request_1"]
    assert trace.links.turn_ids == ["turn_1"]
    assert trace.decisions[0].content_preview == "Use concise answers"
    assert trace.decisions[0].index_status == "pending"
    assert trace.decisions[0].provider_status == "pending"
    assert trace.semantic_contract_version == "v1"
    assert trace.semantic_validation_counts == {"confirmed": 1}
    assert trace.semantic_verifier_counts == {"not_configured": 1}
    serialized = response.model_dump_json()
    assert "trace_summary" not in serialized
    assert "lease_token" not in serialized
    assert response.metadata["trace_models"]["context"] == "context_trace"
    assert response.metadata["trace_models"]["formation"] == "formation_trace"
    recall_response = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        request_id="request_1",
        session_id="session_1",
        turn_id="turn_1",
        run_id="run_1",
    )
    recall_event = next(
        event
        for event in recall_response.events
        if event.event_type == "memory_recall_used"
        and event.payload.get("consumer") == "agent:agent_1"
    )
    assert recall_event.memory_id == added.item.memory_id
    assert recall_event.turn_id == "turn_1"
    assert recall_event.payload == {
        "used_count": 1,
        "revision_id": added.revision.revision_id,
        "consumer": "agent:agent_1",
        "projection_outcome": "included",
        "relevance": 0.91,
        "confidence": 0.95,
    }
    router_events = [
        event
        for event in recall_response.events
        if event.event_type == "memory_recall_used" and event.payload.get("consumer") == "router"
    ]
    assert len(router_events) == 1
    assert router_events[0].turn_id == "turn_1"
    assert {link.consumer for link in recall_response.context_trace_links} == {
        "agent:agent_1",
        "router",
    }
    agent_link = next(
        link for link in recall_response.context_trace_links if link.consumer == "agent:agent_1"
    )
    router_link = next(
        link for link in recall_response.context_trace_links if link.consumer == "router"
    )
    assert (agent_link.relevance, agent_link.confidence) == (0.91, 0.95)
    assert (router_link.relevance, router_link.confidence) == (0.82, 0.95)
    assert recall_response.formation_traces[0].links.turn_ids == ["turn_1"]
    for mismatch in (
        {"request_id": "wrong-request"},
        {"session_id": "wrong-session"},
        {"run_id": "wrong-run"},
        {"formation_job_id": "wrong-job"},
    ):
        mismatched = await observability.debug_state(
            tenant_id="t1",
            user_id="u1",
            turn_id="turn_1",
            **mismatch,
        )
        assert not any(event.payload.get("consumer") == "router" for event in mismatched.events)
    isolated = await observability.debug_state(tenant_id="t2", user_id="u1")
    assert isolated.items == []
    assert isolated.revisions == []
    assert isolated.events == []
    assert isolated.formation_traces == []


async def test_debug_multiple_scopes_use_bounded_or_semantics() -> None:
    items, _, _, observability, _ = _services()
    for scope in ("user_preference", "stable_fact", "task_memory"):
        await items.add_event(
            MemoryEvent(
                event_id=f"event_{scope}",
                event_type="formation_decision",
                user_id="u1",
                tenant_id="t1",
                scope=scope,
            )
        )
    response = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        scopes=["user_preference", "stable_fact"],
    )
    assert {event.scope for event in response.events} == {"user_preference", "stable_fact"}


async def test_recall_only_debug_builds_context_links_without_formation_trace() -> None:
    items, memory, _, observability, _ = _services()
    await items.add(
        MemoryItem(
            memory_id="recall_only",
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="bounded",
            current_revision_id="revision_recall_only",
        )
    )
    item = await items.get_by_id("recall_only", tenant_id="t1")
    assert item
    await memory.record_recall_usage(
        [item.to_context_item(relevance=0.7)],
        user_id="u1",
        tenant_id="t1",
        consumer="router",
        request_id="request_recall_only",
        session_id="session_recall_only",
        turn_id="turn_recall_only",
    )
    response = await observability.debug_state(
        tenant_id="t1", user_id="u1", turn_id="turn_recall_only"
    )
    assert response.formation_traces == []
    assert len(response.context_trace_links) == 1
    assert response.context_trace_links[0].consumer == "router"
    assert response.context_trace_links[0].memory_ids == ["recall_only"]


async def test_mode_off_router_recall_is_linked_to_turn_without_formation_buffer() -> None:
    settings = Settings(storage_backend="memory", memory_formation_mode="off")
    items, memory, formation, observability, _ = _services(settings)
    await items.add(
        MemoryItem(
            memory_id="mode_off_router",
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="bounded",
            current_revision_id="revision_mode_off",
        )
    )
    item = await items.get_by_id("mode_off_router", tenant_id="t1")
    assert item
    await memory.record_recall_usage(
        [item.to_context_item(relevance=0.75)],
        user_id="u1",
        tenant_id="t1",
        consumer="router",
        request_id="request_mode_off",
        session_id="session_mode_off",
    )
    for _ in range(2):
        assert (
            await memory.link_router_recall_usage(
                user_id="u1",
                tenant_id="t1",
                request_id="request_mode_off",
                session_id="session_mode_off",
                run_id="run_mode_off",
                turn_id="turn_mode_off",
            )
            == 1
        )
    assert formation.turns == {}
    response = await observability.debug_state(
        tenant_id="t1", user_id="u1", turn_id="turn_mode_off"
    )
    assert response.formation_traces == []
    assert len(response.context_trace_links) == 1
    assert response.context_trace_links[0].turn_ids == ["turn_mode_off"]
    assert response.context_trace_links[0].run_ids == ["run_mode_off"]
    assert response.context_trace_links[0].consumer == "router"
    assert (await observability.metrics()).recall_used == 1


async def test_health_and_metrics_include_canonical_pipeline_backlog() -> None:
    settings = Settings(storage_backend="memory", memory_formation_mode="enforced")
    items = MemoryItemRepository()
    memory = MemoryService(settings=settings, repository=items)
    formation = MemoryFormationTurnJobRepository()
    traces = MemoryFormationTraceRepository(
        formation_repository=formation,
        event_repository=items,
    )
    turns = MemoryTurnRepository()
    outboxes = MemoryTurnOutboxRepository()
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory,
        formation_repository=formation,
        trace_repository=traces,
        turn_repository=turns,
        outbox_repository=outboxes,
    )
    now = datetime.now(UTC)
    for suffix, status in (
        ("pending", TurnStatus.PENDING),
        ("missing", TurnStatus.COMPLETED),
        ("outbox", TurnStatus.COMPLETED),
    ):
        await turns.create_idempotent(
            CanonicalTurn(
                turn_id=f"turn_{suffix}",
                tenant_id="t1",
                user_id="u1",
                session_id=f"session_{suffix}",
                request_id=f"request_{suffix}",
                source="host_chat",
                status=status,
                user_input=TurnUserInput(text="private metric content"),
                final_response=(
                    TurnSemanticResponse(kind="agent_result", text="private response")
                    if status == TurnStatus.COMPLETED
                    else None
                ),
                created_at=now,
                updated_at=now,
                completed_at=now if status == TurnStatus.COMPLETED else None,
            )
        )
    await outboxes.add_idempotent(
        TurnOutboxEvent(
            outbox_id="outbox_pending",
            turn_id="turn_outbox",
            event_type="turn.completed",
            idempotency_key="turn.completed:turn_outbox",
            available_at=now,
        )
    )

    health = await observability.health()
    metrics = await observability.metrics()

    assert health.pending_turn_count == 1
    assert health.outbox_pending_count == 1
    assert health.trace_missing_count == 1
    assert health.outbox_oldest_pending_seconds is not None
    assert metrics.pending_turn_count == 1
    assert metrics.outbox_pending_count == 1
    assert metrics.trace_missing_count == 1
    assert "private metric content" not in metrics.model_dump_json()
    assert "private response" not in health.model_dump_json()


async def test_debug_service_clamps_internal_limit_to_one_hundred() -> None:
    items, _, _, observability, _ = _services()
    for index in range(150):
        await items.add_event(
            MemoryEvent(
                event_id=f"bounded_event_{index}",
                event_type="bounded_test",
                user_id="u1",
                tenant_id="t1",
            )
        )
    response = await observability.debug_state(tenant_id="t1", user_id="u1", limit=10_000)
    assert len(response.events) == 100


async def test_pending_update_confirm_is_preconditioned_and_idempotent() -> None:
    items, memory, formation, observability, management = _services()
    job = await formation.add_job(
        MemoryFormationJob(
            job_id="job_pending_update",
            trigger="structured_event",
            mode="observe",
            tenant_id="t1",
            user_id="u1",
            idempotency_key="job-pending-update",
            model_version="model-v1",
            prompt_version="prompt-v1",
            policy_version="policy-v1",
        )
    )
    added = await _add_current(memory, job_id=job.job_id)
    pending_result = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(
                operation="update", content="Use detailed answers", confidence=0.8
            ),
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.CONFIDENCE_PENDING,
                memory_id=added.item.memory_id,
                revision_id=added.revision.revision_id,
                job_id=job.job_id,
                suffix="pending-update",
            ),
            redacted_trace={},
        )
    )
    decision_id = pending_result.event.event_id

    try:
        await management.resolve_pending(
            decision_id=decision_id,
            action="confirm",
            tenant_id="t1",
            user_id="u1",
            actor="u1",
            reason="approve correction",
            idempotency_key="confirm-1",
            expected_revision_id="stale-revision",
        )
    except MemoryManagementConflict:
        pass
    else:
        raise AssertionError("stale revision must conflict")

    confirmed = await management.resolve_pending(
        decision_id=decision_id,
        action="confirm",
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="approve correction",
        idempotency_key="confirm-1",
        expected_revision_id=added.revision.revision_id,
    )
    replay = await management.resolve_pending(
        decision_id=decision_id,
        action="confirm",
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="approve correction",
        idempotency_key="confirm-1",
        expected_revision_id=added.revision.revision_id,
    )
    current = await items.get_by_id(added.item.memory_id, tenant_id="t1")
    assert current and current.content == "Use detailed answers"
    assert current.current_revision_no == 2
    assert confirmed.status == "completed"
    assert replay.idempotent_replay is True
    assert (
        len([event for event in items.events if event.event_type == "memory_pending_confirm"]) == 1
    )
    pending_view = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        formation_job_id=job.job_id,
        decision_status="pending",
    )
    resolved_view = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        formation_job_id=job.job_id,
        decision_status="resolved",
    )
    assert pending_view.formation_traces == []
    assert not any(event.event_id == decision_id for event in pending_view.events)
    assert len(resolved_view.formation_traces) == 1
    try:
        await management.resolve_pending(
            decision_id=decision_id,
            action="reject",
            tenant_id="t1",
            user_id="u1",
            actor="u1",
            reason="approve correction",
            idempotency_key="confirm-1",
            expected_revision_id=added.revision.revision_id,
        )
    except MemoryManagementConflict:
        pass
    else:
        raise AssertionError("resolution action changes must conflict")
    try:
        await management.resolve_pending(
            decision_id=decision_id,
            action="confirm",
            tenant_id="t1",
            user_id="u1",
            actor="u1",
            reason="changed payload",
            idempotency_key="confirm-1",
            expected_revision_id=added.revision.revision_id,
        )
    except MemoryManagementConflict:
        pass
    else:
        raise AssertionError("idempotency replay with a changed payload must conflict")


async def test_pending_reject_and_cross_user_access_are_non_disclosing() -> None:
    _, memory, _, _, management = _services()
    added = await _add_current(memory)
    pending = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(
                operation="delete", content="Delete this preference", confidence=0.8
            ),
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.AMBIGUOUS_DELETE,
                memory_id=added.item.memory_id,
                revision_id=added.revision.revision_id,
                suffix="pending-delete",
            ),
            redacted_trace={},
        )
    )
    try:
        await management.resolve_pending(
            decision_id=pending.event.event_id,
            action="reject",
            tenant_id="t1",
            user_id="u2",
            actor="u2",
            reason="reject",
            idempotency_key="reject-1",
            expected_revision_id=None,
        )
    except MemoryManagementNotFound as exc:
        assert str(exc) == "Memory operation target not found"
    else:
        raise AssertionError("cross-user resolution must be denied")
    rejected = await management.resolve_pending(
        decision_id=pending.event.event_id,
        action="reject",
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="keep current memory",
        idempotency_key="reject-1",
        expected_revision_id=None,
    )
    assert rejected.operation == "reject"
    current = await memory.repository.get_by_id(added.item.memory_id, tenant_id="t1")
    assert current and current.lifecycle_status == "active"


async def test_pending_delete_recovers_after_completion_event_crash() -> None:
    items, memory, _, _, management = _services()
    added = await _add_current(memory)
    pending = await _add_pending_delete(memory, added)

    async def crash_before_completion(**_kwargs):
        raise RuntimeError("simulated crash before resolution completion")

    management._complete_resolution = crash_before_completion
    try:
        await management.resolve_pending(
            decision_id=pending.event.event_id,
            action="confirm",
            tenant_id="t1",
            user_id="u1",
            actor="u1",
            reason="confirm deletion",
            idempotency_key="confirm-delete-crash",
            expected_revision_id=added.revision.revision_id,
        )
    except RuntimeError as exc:
        assert str(exc) == "simulated crash before resolution completion"
    else:
        raise AssertionError("completion crash must escape the management call")

    scrubbed = await items.get_event(pending.event.event_id, tenant_id="t1", user_id="u1")
    assert scrubbed and scrubbed.payload["pending_candidate"] == "[redacted]"
    assert scrubbed.decision_status == "pending"
    claim_identity = "pending-resolution-claim\x1f" + pending.event.event_id
    claim = await items.get_event(
        f"mevt_{hashlib.sha256(claim_identity.encode()).hexdigest()[:32]}",
        tenant_id="t1",
        user_id="u1",
    )
    assert claim and claim.decision_status is None
    assert isinstance(claim.payload.get("accepted_operation"), dict)
    assert not any(event.event_type == "memory_pending_confirm" for event in items.events)

    recovered = await MemoryManagementService(memory_service=memory).resolve_pending(
        decision_id=pending.event.event_id,
        action="confirm",
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="confirm deletion",
        idempotency_key="confirm-delete-crash",
        expected_revision_id=added.revision.revision_id,
    )
    operations = await memory.index_outbox.list_for_memory(added.item.memory_id, tenant_id="t1")
    delete_operations = [operation for operation in operations if operation.operation == "delete"]
    assert recovered.status == "completed"
    assert len(delete_operations) == 1
    assert (
        len([event for event in items.events if event.event_type == "memory_pending_confirm"]) == 1
    )


async def test_user_delete_and_admin_delete_record_bounded_audit() -> None:
    items, memory, _, observability, management = _services()
    first = await _add_current(memory)
    deleted = await management.request_delete(
        memory_id=first.item.memory_id,
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="remove my preference",
        idempotency_key="delete-user-1",
        expected_revision_id=first.revision.revision_id,
    )
    replay = await management.request_delete(
        memory_id=first.item.memory_id,
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="remove my preference",
        idempotency_key="delete-user-1",
        expected_revision_id=first.revision.revision_id,
    )
    assert deleted.index_operation_id
    assert replay.idempotent_replay is True
    status = await management.operation_status(
        index_operation_id=deleted.index_operation_id,
        tenant_id="t1",
        user_id="u1",
    )
    assert status.status == "pending"
    debug = await observability.debug_state(tenant_id="t1", user_id="u1")
    assert debug.items[0].lifecycle_status == "deletion_pending"
    try:
        await management.operation_status(
            index_operation_id=deleted.index_operation_id,
            tenant_id="t1",
            user_id="u2",
        )
    except MemoryManagementNotFound:
        pass
    else:
        raise AssertionError("cross-user operation status must be denied")
    audits = [event for event in items.events if event.event_type == "memory_management_audit"]
    assert len(audits) == 1
    assert audits[0].payload["actor"] == "u1"
    assert audits[0].payload["reason"] == "remove my preference"
    assert "delete-user-1" not in str(audits[0].payload)
    assert "Use concise answers" not in str([event.payload for event in items.events])
    try:
        await management.request_delete(
            memory_id=first.item.memory_id,
            tenant_id="t1",
            user_id="u1",
            actor="u1",
            reason="changed reason",
            idempotency_key="delete-user-1",
            expected_revision_id=first.revision.revision_id,
        )
    except MemoryManagementConflict:
        pass
    else:
        raise AssertionError("idempotency replay with a changed payload must conflict")


async def test_hard_deleted_memory_replays_completed_delete_without_current_item() -> None:
    items, memory, _, _, management = _services()
    memory.index_worker.adapter = _CompletingIndexAdapter()
    added = await _add_current(memory)
    assert (await memory.index_worker.run_once()).completed is True
    await items.add_event(
        MemoryEvent(
            event_type="raw_sensitive_payload",
            memory_id=added.item.memory_id,
            tenant_id="t1",
            user_id="u1",
            payload={
                "formation_prompt": "private prompt",
                "supporting_quote": "private quote",
                "nested": {"candidate_quote": "private candidate quote"},
            },
        )
    )
    requested = await management.request_delete(
        memory_id=added.item.memory_id,
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="remove permanently",
        idempotency_key="completed-delete",
        expected_revision_id=added.revision.revision_id,
    )
    assert requested.status == "pending"
    assert (await memory.index_worker.run_once()).completed is True
    assert await memory.repository.get_by_id(added.item.memory_id, tenant_id="t1") is None
    scrubbed = next(event for event in items.events if event.event_type == "raw_sensitive_payload")
    assert scrubbed.payload == {
        "formation_prompt": "[redacted]",
        "supporting_quote": "[redacted]",
        "nested": {"candidate_quote": "[redacted]"},
    }

    replay = await management.request_delete(
        memory_id=added.item.memory_id,
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="remove permanently",
        idempotency_key="completed-delete",
        expected_revision_id=added.revision.revision_id,
    )
    assert replay.status == "completed"
    assert replay.idempotent_replay is True
    try:
        await management.request_delete(
            memory_id=added.item.memory_id,
            tenant_id="t1",
            user_id="u1",
            actor="u1",
            reason="remove permanently",
            idempotency_key="completed-delete",
            expected_revision_id="changed-revision",
        )
    except MemoryManagementConflict:
        pass
    else:
        raise AssertionError("changed completed-delete precondition must conflict")


async def test_database_hard_delete_replay_survives_restart_with_omitted_precondition(
    tmp_path,
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'completed-delete-replay.db'}",
        memory_formation_mode="observe",
    )
    await create_all_tables(settings)
    memory = MemoryService(
        settings=settings,
        repository=DatabaseMemoryItemRepository(create_session_factory(settings)),
    )
    memory.index_worker.adapter = _CompletingIndexAdapter()
    added = await _add_current(memory)
    assert (await memory.index_worker.run_once()).completed is True
    await memory.repository.add_event(
        MemoryEvent(
            event_type="raw_sensitive_payload",
            memory_id=added.item.memory_id,
            tenant_id="t1",
            user_id="u1",
            payload={
                "formation_prompt": "private prompt",
                "supporting_quote": "private quote",
                "nested": {"candidate_quote": "private candidate quote"},
            },
        )
    )
    management = MemoryManagementService(memory_service=memory)
    await management.request_delete(
        memory_id=added.item.memory_id,
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="delete without revision precondition",
        idempotency_key="db-completed-delete",
        expected_revision_id=None,
    )
    assert (await memory.index_worker.run_once()).completed is True

    scrubbed = await memory.repository.list_events(
        tenant_id="t1", user_id="u1", memory_id=added.item.memory_id, limit=100
    )
    sensitive = next(event for event in scrubbed if event.event_type == "raw_sensitive_payload")
    assert sensitive.payload == {
        "formation_prompt": "[redacted]",
        "supporting_quote": "[redacted]",
        "nested": {"candidate_quote": "[redacted]"},
    }

    restarted = MemoryService(
        settings=settings,
        repository=DatabaseMemoryItemRepository(create_session_factory(settings)),
    )
    replay = await MemoryManagementService(memory_service=restarted).request_delete(
        memory_id=added.item.memory_id,
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="delete without revision precondition",
        idempotency_key="db-completed-delete",
        expected_revision_id=None,
    )
    assert replay.status == "completed"
    assert replay.idempotent_replay is True


async def test_debug_redacts_secrets_regulated_values_and_internal_job_payload() -> None:
    items, _, formation, observability, _ = _services()
    secret = "Bearer abcdefghijklmnopqrstuvwxyz"
    regulated = "11010519491231002X"
    email = "private.person@example.test"
    ssn = "123-45-6789"
    await items.add(
        MemoryItem(
            memory_id="mem_secret",
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content=f"Authorization: {secret} ID {regulated}",
            structured_value={"password": "open-sesame", "token_estimate": 12},
            metadata={"database_url": "postgresql://user:pass@host/db"},
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="provider_error",
            memory_id="mem_secret",
            user_id="u1",
            tenant_id="t1",
            payload={
                "error": (
                    "postgresql://db-user:db-pass@host/db?token=uri-secret "
                    f"authorization={secret} {email} {ssn}"
                ),
                "formation_prompt": "private prompt",
                "supporting_quote": "private quote",
            },
        )
    )
    await formation.add_job(
        MemoryFormationJob(
            trigger="structured_event",
            mode="observe",
            tenant_id="t1",
            user_id="u1",
            idempotency_key="job-secret",
            model_version="model-v1",
            prompt_version="prompt-v1",
            policy_version="policy-v1",
            trace_summary={"command": {"candidates": [{"content": secret}]}},
        )
    )
    response = await observability.debug_state(tenant_id="t1", user_id="u1")
    serialized = response.model_dump_json()
    assert secret not in serialized
    assert regulated not in serialized
    assert email not in serialized
    assert ssn not in serialized
    assert "db-user:db-pass" not in serialized
    assert "uri-secret" not in serialized
    assert "open-sesame" not in serialized
    assert "postgresql://user:pass" not in serialized
    assert "private prompt" not in serialized
    assert "private quote" not in serialized
    assert response.items[0].structured_value["token_estimate"] == 12


async def test_pending_semantic_and_structured_values_are_not_exposed_by_debug() -> None:
    _, memory, _, observability, _ = _services()
    marker = "PRIVATE-SEMANTIC-VALUE-7429"
    candidate = _candidate(operation="update", content="Update the stored preference").model_copy(
        update={
            "structured_value": {"slot": "response_style", "value": marker},
            "semantic": MemoryCandidateSemantics(
                target="assistant_response",
                slot="response_style",
                value=marker,
                temporal_scope="long_term",
                polarity="affirmed",
                certainty="uncertain",
                change_intent="replace",
            ),
        }
    )
    pending = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=candidate,
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.AMBIGUOUS_CONFLICT,
                suffix="private-semantic",
            ),
            redacted_trace={},
        )
    )
    assert pending.event is not None
    stored = next(
        event for event in memory.repository.events if event.event_id == pending.event.event_id
    )
    assert stored.payload["pending_candidate"]["semantic"]["value"] == marker

    response = await observability.debug_state(tenant_id="t1", user_id="u1")
    serialized = response.model_dump_json()

    assert marker not in serialized
    public_event = next(
        event for event in response.events if event.event_id == pending.event.event_id
    )
    public_candidate = public_event.payload["pending_candidate"]
    assert "structured_value" not in public_candidate
    assert "value" not in public_candidate["semantic"]


async def test_trace_hydration_keeps_pending_add_and_sensitive_decisions_non_confirmable() -> None:
    _, memory, formation, observability, _ = _services()
    job = await formation.add_job(
        MemoryFormationJob(
            job_id="job_non_confirmable_pending",
            trigger="structured_event",
            mode="observe",
            tenant_id="t1",
            user_id="u1",
            idempotency_key="job-non-confirmable-pending",
            model_version="model-v1",
            prompt_version="prompt-v1",
            policy_version="policy-v1",
        )
    )
    pending_add = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(operation="add", content="Candidate to reject", confidence=0.8),
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.AMBIGUOUS_CONFLICT,
                job_id=job.job_id,
                suffix="pending-add",
            ),
            redacted_trace={},
        )
    )
    sensitive_marker = "PRIVATE-SENSITIVE-CANDIDATE-8842"
    sensitive = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(operation="update", content=sensitive_marker, confidence=0.8),
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.SENSITIVE_CONTENT,
                job_id=job.job_id,
                suffix="pending-sensitive",
            ),
            redacted_trace={},
        )
    )

    response = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        formation_job_id=job.job_id,
        decision_status="pending",
        limit=1,
    )
    decisions = {
        decision.operation_id: decision for decision in response.formation_traces[0].decisions
    }
    add_decision = decisions["operation_pending-add"]
    sensitive_decision = decisions["operation_pending-sensitive"]

    assert add_decision.decision_id == pending_add.event.event_id
    assert add_decision.proposed_operation is None
    assert sensitive_decision.decision_id == sensitive.event.event_id
    assert sensitive_decision.proposed_operation is None
    assert sensitive_decision.content_redacted is True
    assert sensitive_marker not in response.model_dump_json()


async def test_health_and_metrics_are_content_free() -> None:
    items, memory, formation, observability, _ = _services()
    now = datetime.now(UTC)
    idle_job = await formation.add_job(
        MemoryFormationJob(
            job_id="job_metric_idle",
            trigger="idle",
            mode="observe",
            tenant_id="t1",
            user_id="u1",
            session_id="s1",
            idempotency_key="pending-job",
            model_version="model-v1",
            prompt_version="prompt-v1",
            policy_version="policy-v1",
            attempt_count=2,
            trace_summary={
                "job_latency_ms": 50,
                "queue_latency_ms": 9,
                "model_latency_ms": 42,
                "scopes": ["user_preference"],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "private.person@example.test": 999,
                },
                "content": "must never become a metric label",
            },
            created_at=now,
            updated_at=now,
        )
    )
    window_job = await formation.add_job(
        MemoryFormationJob(
            job_id="job_metric_window",
            trigger="turn_window",
            mode="observe",
            status="completed",
            tenant_id="t1",
            user_id="u1",
            session_id="s2",
            idempotency_key="completed-window-job",
            model_version="model-v1",
            prompt_version="prompt-v1",
            policy_version="policy-v1",
            trace_summary={
                "queue_latency_ms": 4,
                "model_latency_ms": 10,
                "scopes": ["stable_fact"],
                "usage": {"input_tokens": 2},
            },
            created_at=now,
            updated_at=now,
        )
    )
    await items.add(
        MemoryItem(
            memory_id="mem_out_of_sync",
            scope="stable_fact",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="private metric content",
            index_status="out_of_sync",
        )
    )
    await memory.index_outbox.add(
        MemoryIndexOperation(
            idempotency_key="index-op",
            operation="update",
            memory_id="mem_out_of_sync",
            tenant_id="t1",
            status="retry",
            attempt_count=2,
            last_error_code="provider_timeout",
            last_error_metadata={"provider_latency_ms": 11},
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="memory_recall_used",
            tenant_id="t1",
            user_id="u1",
            payload={"used_count": 2},
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="memory_index_repair",
            tenant_id="t1",
            payload={"status": "ok", "latency_ms": 13, "rebuild": False},
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="memory_index_repair",
            tenant_id="t1",
            payload={
                "status": "ok",
                "latency_ms": 7,
                "rebuild": False,
                "added_count": 2,
                "updated_count": 3,
                "adopted_count": 1,
                "orphan_deleted_count": 4,
            },
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="memory_deletion_retry",
            tenant_id="t1",
            payload={"error_code": "provider_timeout"},
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="memory_deletion_dead_letter",
            tenant_id="t1",
            payload={"error_code": "provider_timeout"},
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="memory_decision_add",
            tenant_id="t1",
            user_id="u1",
            formation_job_id=idle_job.job_id,
            decision_status="accepted",
            scope="user_preference",
        )
    )
    await items.add_event(
        MemoryEvent(
            event_type="memory_decision_noop",
            tenant_id="t1",
            user_id="u1",
            formation_job_id=window_job.job_id,
            decision_status="accepted",
            scope="stable_fact",
        )
    )
    health = await observability.health()
    metrics = await observability.metrics()
    assert health.queue_depth == 1
    assert health.index_out_of_sync_count == 1
    assert health.last_safe_error == "provider_timeout"
    assert metrics.retries == 1
    assert metrics.snapshot_limit == 10_000
    assert metrics.queue_latency_ms_total == 13
    assert metrics.jobs_by_trigger_scope_status == {
        "idle:user_preference:pending": 1,
        "turn_window:stable_fact:completed": 1,
    }
    assert metrics.formation_series["idle:user_preference:pending"].model_usage_total == {
        "input_tokens": 7,
        "output_tokens": 3,
    }
    assert metrics.formation_series[
        "idle:user_preference:pending"
    ].decisions_by_operation_status == {"add:accepted": 1}
    assert metrics.formation_series["turn_window:stable_fact:completed"].model_usage_total == {
        "input_tokens": 2
    }
    assert metrics.formation_series[
        "turn_window:stable_fact:completed"
    ].decisions_by_operation_status == {"noop:accepted": 1}
    assert metrics.job_latency_ms_total == 50
    assert metrics.model_latency_ms_total == 52
    assert metrics.model_usage_total == {"input_tokens": 9, "output_tokens": 3}
    assert metrics.decision_rates == {"add:accepted": 0.5, "noop:accepted": 0.5}
    assert metrics.recall_used == 2
    assert metrics.index_operation_latency_ms_total == 11
    assert metrics.index_out_of_sync_count == 1
    assert metrics.index_repairs == 2
    assert metrics.index_repair_latency_ms_total == 20
    assert metrics.index_repaired_records == 6
    assert metrics.index_orphan_records == 4
    assert metrics.deletion_failures == 2
    assert metrics.deletion_dead_letters == 1
    assert "private metric content" not in metrics.model_dump_json()
    assert "must never become a metric label" not in metrics.model_dump_json()


async def test_memory_management_api_requires_identity_and_hides_cross_user_target() -> None:
    _, memory, _, observability, management = _services()
    added = await _add_current(memory)
    settings = Settings(
        app_env="production",
        storage_backend="memory",
        admin_api_token="admin",
        memory_identity_secret=_MEMORY_IDENTITY_SECRET,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_memory_management_service] = lambda: management
    app.dependency_overrides[get_memory_observability_service] = lambda: observability
    client = TestClient(app)
    payload = {
        "idempotency_key": "api-delete-1",
        "reason": "remove",
        "expected_revision_id": added.revision.revision_id,
    }
    assert (
        client.request(
            "DELETE", f"/api/v1/memories/{added.item.memory_id}", json=payload
        ).status_code
        == 401
    )
    forged_identity = client.request(
        "DELETE",
        f"/api/v1/memories/{added.item.memory_id}",
        headers={"X-User-ID": "u1", "X-Tenant-ID": "t1"},
        json=payload,
    )
    assert forged_identity.status_code == 401
    denied = client.request(
        "DELETE",
        f"/api/v1/memories/{added.item.memory_id}",
        headers=_memory_actor_headers("u2", "t1"),
        json=payload,
    )
    assert denied.status_code == 404
    assert denied.json()["detail"] == "Memory operation target not found"
    allowed = client.request(
        "DELETE",
        f"/api/v1/memories/{added.item.memory_id}",
        headers=_memory_actor_headers("u1", "t1"),
        json=payload,
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "pending"


async def test_debug_and_admin_api_authorization_boundaries() -> None:
    _, memory, _, observability, management = _services()
    await _add_current(memory)
    settings = Settings(
        app_env="production",
        storage_backend="memory",
        admin_api_token="admin",
        memory_identity_secret=_MEMORY_IDENTITY_SECRET,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_memory_management_service] = lambda: management
    app.dependency_overrides[get_memory_observability_service] = lambda: observability
    client = TestClient(app)
    assert client.get("/api/v1/memories/debug").status_code == 401
    denied = client.get(
        "/api/v1/memories/debug?user_id=u2&tenant_id=t1",
        headers=_memory_actor_headers("u1", "t1"),
    )
    assert denied.status_code == 404
    owned = client.get(
        "/api/v1/memories/debug?user_id=u1&tenant_id=t1",
        headers=_memory_actor_headers("u1", "t1"),
    )
    assert owned.status_code == 200
    assert len(owned.json()["items"]) == 1
    assert client.get("/api/v1/admin/memories/health").status_code == 401
    admin = client.get(
        "/api/v1/admin/memories/debug?tenant_id=t1&user_id=u1",
        headers={"X-Admin-Token": "admin"},
    )
    assert admin.status_code == 200
    assert (
        client.get(
            "/api/v1/admin/memories/debug?tenant_id=t1&limit=0",
            headers={"X-Admin-Token": "admin"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/admin/memories/debug?tenant_id=t1&limit=101",
            headers={"X-Admin-Token": "admin"},
        ).status_code
        == 422
    )
    metrics = client.get("/api/v1/admin/memories/metrics", headers={"X-Admin-Token": "admin"})
    assert metrics.status_code == 200


def test_local_debug_requires_tenant_for_formation_filters() -> None:
    _, memory, _, observability, management = _services()
    settings = Settings(app_env="local", storage_backend="memory")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_memory_service] = lambda: memory
    app.dependency_overrides[get_memory_management_service] = lambda: management
    app.dependency_overrides[get_memory_observability_service] = lambda: observability
    client = TestClient(app)

    for name in (
        "memory_id",
        "request_id",
        "session_id",
        "turn_id",
        "run_id",
        "formation_job_id",
        "memory_key",
        "decision_status",
    ):
        response = client.get("/api/v1/memories/debug", params={name: "filter-value"})
        assert response.status_code == 400, name
        assert response.json()["detail"] == (
            "tenant_id is required for formation and lifecycle debug filters"
        )


async def test_admin_cross_subject_delete_requires_token_and_records_actor_reason() -> None:
    items, _, _, observability, management = _services()
    await items.add(
        MemoryItem(
            memory_id="mem_u2",
            scope="stable_fact",
            subject_id="u2",
            user_id="u2",
            tenant_id="t2",
            content="owned by user two",
            memory_key="tenant:t2:user:u2:fact:timezone",
            candidate_hash="sha256:u2",
            current_revision_id="revision_u2",
            current_revision_no=1,
            index_status="ready",
        )
    )
    settings = Settings(
        app_env="production",
        storage_backend="memory",
        admin_api_token="admin",
        admin_actor_id="admin-service",
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_memory_management_service] = lambda: management
    app.dependency_overrides[get_memory_observability_service] = lambda: observability
    client = TestClient(app)
    payload = {
        "tenant_id": "t2",
        "user_id": "u2",
        "actor": "forged-admin@example.test",
        "reason": "privacy request 42",
        "idempotency_key": "admin-delete-u2",
        "expected_revision_id": "revision_u2",
    }
    path = "/api/v1/admin/memories/mem_u2/delete"
    assert client.post(path, json=payload).status_code == 401
    response = client.post(path, headers={"X-Admin-Token": "admin"}, json=payload)
    assert response.status_code == 200
    audit = next(event for event in items.events if event.event_type == "memory_management_audit")
    assert audit.payload["actor"] == "admin-service"
    assert audit.payload["target_user_id"] == "u2"
    assert audit.payload["reason"] == "privacy request 42"
    assert audit.payload["admin"] is True


async def test_database_health_uses_aggregates_without_loading_event_ledger(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'health-aggregate.db'}",
        memory_formation_mode="observe",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    items = DatabaseMemoryItemRepository(session_factory)
    await items.add_event(
        MemoryEvent(
            event_type="large_ledger_marker",
            tenant_id="t1",
            payload={"content": "health must not load this payload"},
        )
    )
    memory = MemoryService(settings=settings, repository=items)
    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory,
        formation_repository=formation,
        trace_repository=DatabaseMemoryFormationTraceRepository(session_factory),
    )
    statements = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = session_factory.kw["bind"]
    sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        health = await observability.health()
    finally:
        sqlalchemy_event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)
    assert health.queue_depth == 0
    assert statements
    assert not any("memory_events" in statement for statement in statements)


async def test_database_metrics_are_full_aggregates_beyond_snapshot_limit(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'metrics-aggregate.db'}",
        memory_formation_mode="observe",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    async with session_factory() as session:
        await session.execute(
            text(
                """
                WITH RECURSIVE sequence(value) AS (
                    SELECT 1
                    UNION ALL
                    SELECT value + 1 FROM sequence WHERE value < 10001
                )
                INSERT INTO memory_formation_jobs (
                    job_id, trigger, status, mode, tenant_id, user_id,
                    source_refs_text, idempotency_key, model_version, prompt_version,
                    policy_version, attempt_count, max_attempts, trace_summary_text,
                    created_at, updated_at
                )
                SELECT 'bulk_job_' || value, 'idle', 'completed', 'observe', 't1', 'u1',
                       '[]', 'bulk-key-' || value, 'model-v1', 'prompt-v1', 'policy-v1',
                       0, 5, :trace_summary,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM sequence
                """
            ),
            {"trace_summary": ('{"scopes":["stable_fact"],"usage":{"input_tokens":1}}')},
        )
        await session.execute(
            text(
                """
                WITH RECURSIVE sequence(value) AS (
                    SELECT 1
                    UNION ALL
                    SELECT value + 1 FROM sequence WHERE value < 10001
                )
                INSERT INTO memory_events (
                    event_id, event_type, tenant_id, user_id, formation_job_id,
                    decision_status, scope, payload_text, created_at
                )
                SELECT 'bulk_decision_' || value, 'memory_decision_add', 't1', 'u1',
                       'bulk_job_' || value, 'accepted', 'stable_fact', '{}', CURRENT_TIMESTAMP
                FROM sequence
                """
            )
        )
        await session.execute(
            text(
                """
                WITH RECURSIVE sequence(value) AS (
                    SELECT 1
                    UNION ALL
                    SELECT value + 1 FROM sequence WHERE value < 10001
                )
                INSERT INTO memory_events (
                    event_id, event_type, tenant_id, payload_text, created_at
                )
                SELECT 'bulk_tombstone_' || value, 'memory_deleted_tombstone', 't1', '{}',
                       CURRENT_TIMESTAMP
                FROM sequence
                """
            )
        )
        await session.commit()

    items = DatabaseMemoryItemRepository(session_factory)
    memory = MemoryService(settings=settings, repository=items)
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory,
        formation_repository=DatabaseMemoryFormationTurnJobRepository(session_factory),
        trace_repository=DatabaseMemoryFormationTraceRepository(session_factory),
    )
    statements = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = session_factory.kw["bind"]
    sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        metrics = await observability.metrics()
    finally:
        sqlalchemy_event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

    assert metrics.aggregation_mode == "full_database"
    assert metrics.snapshot_limit is None
    assert metrics.jobs_by_trigger_status == {"idle:completed": 10001}
    assert metrics.jobs_by_trigger_scope_status == {"idle:stable_fact:completed": 10001}
    assert metrics.decisions_by_operation_status == {"add:accepted": 10001}
    assert metrics.formation_series["idle:stable_fact:completed"].decisions_by_operation_status == {
        "add:accepted": 10001
    }
    assert metrics.model_usage_total == {"input_tokens": 10001}
    assert metrics.deletion_completions == 10001
    assert statements
    assert not any("limit 10000" in statement for statement in statements)


async def test_database_association_filters_hydrate_job_level_pending_decision(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'job-decision-association.db'}",
        memory_formation_mode="observe",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    items = DatabaseMemoryItemRepository(session_factory)
    memory = MemoryService(settings=settings, repository=items)
    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
    now = datetime(2026, 7, 13, tzinfo=UTC)
    for suffix in ("first", "second"):
        await formation.append_turn(
            MemoryFormationTurn(
                turn_id=f"turn_association_{suffix}",
                request_id=f"request_association_{suffix}",
                session_id="session_association",
                run_id=f"run_association_{suffix}",
                user_id="u1",
                tenant_id="t1",
                result_status="completed",
                completed_at=now,
            )
        )
    job = await formation.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="session_association",
        trigger="idle",
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    added = await _add_current(memory, job_id=job.job_id)
    pending = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(
                operation="update", content="Association-safe update", confidence=0.8
            ),
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.CONFIDENCE_PENDING,
                memory_id=added.item.memory_id,
                revision_id=added.revision.revision_id,
                job_id=job.job_id,
                suffix="association-pending-update",
            ),
            redacted_trace={},
        )
    )
    assert pending.event.request_id is None
    assert pending.event.session_id is None
    assert pending.event.turn_id is None
    assert pending.event.run_id is None

    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory,
        formation_repository=formation,
        trace_repository=DatabaseMemoryFormationTraceRepository(session_factory),
    )
    association_filters = (
        {"request_id": "request_association_first"},
        {"session_id": "session_association"},
        {"turn_id": "turn_association_second"},
        {"run_id": "run_association_second"},
    )
    for filters in association_filters:
        response = await observability.debug_state(
            tenant_id="t1",
            user_id="u1",
            limit=1,
            **filters,
        )
        assert len(response.formation_traces) == 1
        decision = next(
            value
            for value in response.formation_traces[0].decisions
            if value.operation_id == "operation_association-pending-update"
        )
        assert decision.decision_id == pending.event.event_id
        assert decision.proposed_operation == "update"
        assert all(event.event_id != pending.event.event_id for event in response.events)

    assert (
        await observability.debug_state(
            tenant_id="t1",
            user_id="u2",
            request_id="request_association_first",
        )
    ).formation_traces == []
    assert (
        await observability.debug_state(
            tenant_id="t2",
            user_id="u1",
            request_id="request_association_first",
        )
    ).formation_traces == []


async def test_database_pending_resolution_and_trace_survive_service_restart(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'management-restart.db'}",
        memory_formation_mode="observe",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    items = DatabaseMemoryItemRepository(session_factory)
    memory = MemoryService(settings=settings, repository=items)
    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
    now = datetime(2026, 7, 13, tzinfo=UTC)
    await formation.append_turn(
        MemoryFormationTurn(
            turn_id="turn_db",
            request_id="request_db",
            session_id="session_db",
            run_id="run_db",
            user_id="u1",
            tenant_id="t1",
            result_status="completed",
            completed_at=now,
        )
    )
    job = await formation.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="session_db",
        trigger="idle",
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    added = await _add_current(memory, job_id=job.job_id)
    pending = await memory.lifecycle.apply(
        CandidatePolicyResult(
            candidate=_candidate(operation="update", content="Persisted update", confidence=0.8),
            operation=_operation(
                operation=MemoryOperation.PENDING,
                status=MemoryDecisionStatus.PENDING,
                reason=MemoryFormationReasonCode.CONFIDENCE_PENDING,
                memory_id=added.item.memory_id,
                revision_id=added.revision.revision_id,
                job_id=job.job_id,
                suffix="db-pending",
            ),
            redacted_trace={},
        )
    )

    restarted_items = DatabaseMemoryItemRepository(create_session_factory(settings))
    restarted_memory = MemoryService(settings=settings, repository=restarted_items)
    restarted_formation = DatabaseMemoryFormationTurnJobRepository(create_session_factory(settings))
    traces = DatabaseMemoryFormationTraceRepository(create_session_factory(settings))
    management = MemoryManagementService(memory_service=restarted_memory)
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=restarted_memory,
        formation_repository=restarted_formation,
        trace_repository=traces,
    )
    pending_debug = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        formation_job_id=job.job_id,
        decision_status="pending",
    )
    pending_decision = pending_debug.formation_traces[0].decisions[0]
    assert pending_decision.decision_id == pending.event.event_id
    assert pending_decision.proposed_operation == "update"
    confirmed = await management.resolve_pending(
        decision_id=pending.event.event_id,
        action="confirm",
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="confirm after restart",
        idempotency_key="db-confirm",
        expected_revision_id=added.revision.revision_id,
    )
    assert confirmed.index_operation_id
    status = await management.operation_status(
        index_operation_id=confirmed.index_operation_id,
        tenant_id="t1",
        user_id="u1",
    )
    assert status.status == "pending"
    debug = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        request_id="request_db",
        session_id="session_db",
        turn_id="turn_db",
        run_id="run_db",
        formation_job_id=job.job_id,
        memory_id=added.item.memory_id,
    )
    assert len(debug.formation_traces) == 1
    assert debug.formation_traces[0].links.turn_ids == ["turn_db"]
    assert any(
        decision.operation == "pending" and decision.decision_status == "resolved"
        for decision in debug.formation_traces[0].decisions
    )
    assert any(
        decision.operation == "update" and decision.decision_status == "accepted"
        for decision in debug.formation_traces[0].decisions
    )
    assert debug.items[0].content == "Persisted update"
    assert debug.items[0].current_revision_id != added.revision.revision_id
    assert [revision.revision_no for revision in debug.revisions] == [1, 2]
    resolved = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        formation_job_id=job.job_id,
        decision_status="resolved",
    )
    assert len(resolved.formation_traces) == 1
    still_pending = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        formation_job_id=job.job_id,
        decision_status="pending",
    )
    assert still_pending.formation_traces == []
    assert not any(event.event_id == pending.event.event_id for event in still_pending.events)
    assert await traces.list_traces(tenant_id="t2", memory_id=added.item.memory_id) == []


async def test_database_pending_delete_recovers_after_claim_only_restart(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'delete-resolution-crash.db'}",
        memory_formation_mode="observe",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    items = DatabaseMemoryItemRepository(session_factory)
    memory = MemoryService(settings=settings, repository=items)
    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
    now = datetime(2026, 7, 13, tzinfo=UTC)
    await formation.append_turn(
        MemoryFormationTurn(
            turn_id="turn_delete_crash",
            request_id="request_delete_crash",
            session_id="session_delete_crash",
            run_id="run_delete_crash",
            user_id="u1",
            tenant_id="t1",
            result_status="completed",
            completed_at=now,
        )
    )
    job = await formation.create_job_for_pending(
        tenant_id="t1",
        user_id="u1",
        session_id="session_delete_crash",
        trigger="idle",
        mode="observe",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    assert job
    added = await _add_current(memory, job_id=job.job_id)
    pending = await _add_pending_delete(memory, added, job_id=job.job_id)
    management = MemoryManagementService(memory_service=memory)

    async def crash_before_completion(**_kwargs):
        raise RuntimeError("simulated process exit")

    management._complete_resolution = crash_before_completion
    try:
        await management.resolve_pending(
            decision_id=pending.event.event_id,
            action="confirm",
            tenant_id="t1",
            user_id="u1",
            actor="u1",
            reason="confirm persisted deletion",
            idempotency_key="db-delete-crash",
            expected_revision_id=added.revision.revision_id,
        )
    except RuntimeError as exc:
        assert str(exc) == "simulated process exit"
    else:
        raise AssertionError("completion crash must escape the management call")

    restarted_items = DatabaseMemoryItemRepository(create_session_factory(settings))
    restarted_memory = MemoryService(settings=settings, repository=restarted_items)
    restarted_formation = DatabaseMemoryFormationTurnJobRepository(create_session_factory(settings))
    traces = DatabaseMemoryFormationTraceRepository(create_session_factory(settings))
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=restarted_memory,
        formation_repository=restarted_formation,
        trace_repository=traces,
    )
    scrubbed = await restarted_items.get_event(pending.event.event_id, tenant_id="t1", user_id="u1")
    assert scrubbed and scrubbed.payload["pending_candidate"] == "[redacted]"
    before_retry = await observability.debug_state(
        tenant_id="t1",
        user_id="u1",
        formation_job_id=job.job_id,
    )
    assert any(
        decision.operation == "pending" and decision.decision_status == "pending"
        for decision in before_retry.formation_traces[0].decisions
    )
    assert not any(
        decision.operation == "pending" and decision.decision_status == "resolved"
        for decision in before_retry.formation_traces[0].decisions
    )

    recovered = await MemoryManagementService(memory_service=restarted_memory).resolve_pending(
        decision_id=pending.event.event_id,
        action="confirm",
        tenant_id="t1",
        user_id="u1",
        actor="u1",
        reason="confirm persisted deletion",
        idempotency_key="db-delete-crash",
        expected_revision_id=added.revision.revision_id,
    )
    operations = await restarted_memory.index_outbox.list_for_memory(
        added.item.memory_id, tenant_id="t1"
    )
    events = await restarted_items.list_events(
        tenant_id="t1", user_id="u1", memory_id=added.item.memory_id, limit=100
    )
    assert recovered.status == "completed"
    assert len([operation for operation in operations if operation.operation == "delete"]) == 1
    assert len([event for event in events if event.event_type == "memory_pending_confirm"]) == 1
