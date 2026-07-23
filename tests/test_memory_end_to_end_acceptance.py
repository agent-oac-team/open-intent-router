from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.llm.conversation_formation import (
    ConversationFormationResponse,
    FakeConversationFormationModel,
)
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.execution_traces import MemoryExecutionTraceRepository
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.repositories.memory_traces import MemoryFormationTraceRepository
from app.repositories.turns import MemoryTurnRepository
from app.schemas.common import UserContext
from app.schemas.execution_traces import ExecutionTraceQuery
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTurn,
    MemoryItem,
    MemoryRecallRequest,
    MemorySemanticVerification,
)
from app.schemas.turns import TurnUserInput
from app.services.execution_trace_service import ExecutionTraceService
from app.services.memory_candidate_policy import MemoryCandidatePolicy, build_memory_key
from app.services.memory_integration import MemoryFormationProcessor
from app.services.memory_lifecycle import MemoryConsolidationService
from app.services.memory_management import MemoryManagementNotFound, MemoryManagementService
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService
from app.services.turn_service import TurnService


def _settings() -> Settings:
    return Settings(
        storage_backend="memory",
        memory_strategy_provider="memory",
        memory_mode="on",
        memory_formation_model_timeout_seconds=1,
        memory_formation_lease_seconds=5,
    )


def _turn(*, tenant_id: str, user_id: str, suffix: str, content: str) -> MemoryFormationTurn:
    return MemoryFormationTurn(
        turn_id=f"turn_{suffix}",
        request_id=f"request_{suffix}",
        session_id=f"session_{tenant_id}",
        run_id=f"run_{suffix}",
        user_id=user_id,
        tenant_id=tenant_id,
        agent_id="acceptance_agent",
        user_text=content,
        assistant_text="acknowledged",
        result_status="completed",
        completed_at=datetime(2026, 7, 14, tzinfo=UTC),
    )


def _job(*, tenant_id: str, user_id: str, suffix: str, turn_id: str) -> MemoryFormationJob:
    return MemoryFormationJob(
        job_id=f"job_{suffix}",
        trigger="manual",
        mode="enforced",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=f"session_{tenant_id}",
        source_refs=[turn_id],
        idempotency_key=f"acceptance:{tenant_id}:{user_id}:{suffix}",
        model_version="acceptance-model-v1",
        prompt_version="acceptance-prompt-v1",
        policy_version="acceptance-policy-v1",
    )


def _candidate(
    *,
    tenant_id: str,
    user_id: str,
    suffix: str,
    slot: str,
    content: str,
    evidence_text: str,
) -> MemoryFormationCandidate:
    return MemoryFormationCandidate(
        candidate_id=f"candidate_{suffix}",
        proposed_operation="add",
        scope="user_preference",
        content=content,
        structured_value={"slot": slot, "value": content},
        semantic=MemoryCandidateSemantics(
            target="assistant_response",
            slot=slot,
            value=content,
            temporal_scope="long_term",
            polarity="affirmed",
            certainty="certain",
            change_intent="set",
        ),
        subject_id_hint=user_id,
        tenant_id_hint=tenant_id,
        memory_key_hint=slot,
        confidence=0.99,
        evidence_refs=[
            MemoryEvidenceRef(turn_id=f"turn_{suffix}", role="user", quote=evidence_text)
        ],
        reason="explicit long-term preference",
    )


async def _form(
    *,
    formation: MemoryFormationTurnJobRepository,
    policy: MemoryCandidatePolicy,
    service: MemoryService,
    tenant_id: str,
    user_id: str,
    suffix: str,
    slot: str,
    content: str,
):
    evidence_text = "以后请使用简洁风格回答"
    turn = _turn(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=suffix,
        content=evidence_text,
    )
    job = _job(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=suffix,
        turn_id=turn.turn_id,
    )
    await formation.append_turn(turn)
    await formation.add_job(job)
    decision = await policy.evaluate(
        job=job,
        candidate=_candidate(
            tenant_id=tenant_id,
            user_id=user_id,
            suffix=suffix,
            slot=slot,
            content=content,
            evidence_text=evidence_text,
        ),
        turns=[turn],
    )
    return await service.lifecycle.apply(decision)


async def _drain_index(service: MemoryService) -> None:
    for _ in range(100):
        if await service.index_worker.run_once() is None:
            return
    raise AssertionError("index outbox did not drain")


@pytest.mark.asyncio
async def test_memory_processor_projects_bounded_execution_trace_for_canonical_turn() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    memory_service = MemoryService(
        settings=settings,
        repository=memories,
        formation_repository=formation,
    )
    turns = TurnService(MemoryTurnRepository())
    started = await turns.start_turn(
        tenant_id="tenant-trace",
        user_id="user-trace",
        session_id="session-trace",
        request_id="request-trace",
        source="host_chat",
        user_input=TurnUserInput(text="请以后用中文回答"),
    )
    canonical = started.turn
    formation_turn = MemoryFormationTurn(
        turn_id=canonical.turn_id,
        request_id=canonical.request_id,
        session_id=canonical.session_id,
        user_id=canonical.user_id,
        tenant_id=canonical.tenant_id,
        user_text="请以后用中文回答",
        assistant_text="已记录",
        result_status="completed",
    )
    job = MemoryFormationJob(
        job_id="job-trace",
        trigger="manual",
        mode="enforced",
        tenant_id=canonical.tenant_id,
        user_id=canonical.user_id,
        session_id=canonical.session_id,
        first_turn_id=canonical.turn_id,
        last_turn_id=canonical.turn_id,
        source_refs=[canonical.turn_id],
        idempotency_key="trace-memory-formation",
        model_version="test-model",
        prompt_version="test-prompt",
        policy_version="test-policy",
    )
    await formation.append_turn(formation_turn)
    await formation.add_job(job)
    trace_service = ExecutionTraceService(MemoryExecutionTraceRepository())
    candidate = MemoryFormationCandidate(
        candidate_id="candidate-trace",
        proposed_operation="add",
        scope="user_preference",
        content="用户偏好中文回复",
        structured_value={"slot": "response_language", "value": "zh"},
        semantic=MemoryCandidateSemantics(
            target="assistant_response",
            slot="response_language",
            value="zh",
            temporal_scope="long_term",
            polarity="affirmed",
            certainty="certain",
            change_intent="set",
        ),
        subject_id_hint=canonical.user_id,
        tenant_id_hint=canonical.tenant_id,
        memory_key_hint="response_language",
        confidence=0.99,
        evidence_refs=[
            MemoryEvidenceRef(turn_id=canonical.turn_id, role="user", quote="请以后用中文回答")
        ],
        reason="explicit preference",
    )
    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=memories,
        model=FakeConversationFormationModel(ConversationFormationResponse(candidates=[candidate])),
        policy=MemoryCandidatePolicy(settings=settings, repository=memories),
        lifecycle=memory_service.lifecycle,
        execution_traces=trace_service,
        turns=turns,
    )

    await processor.process(job, execute_lifecycle=True)

    snapshot = await trace_service.snapshot(
        ExecutionTraceQuery(
            tenant_id=canonical.tenant_id,
            user_id=canonical.user_id,
            session_id=canonical.session_id,
            turn_id=canonical.turn_id,
        )
    )
    assert [event.event_type for event in snapshot.events] == [
        "memory_formation",
        "memory_decision",
        "memory_revision",
    ]
    decision = snapshot.events[1]
    revision = snapshot.events[2]
    assert decision.facts["operation"] == "add"
    assert decision.facts["decision_status"] == "accepted"
    assert revision.facts["index_status"] == "pending"
    assert "用户偏好中文回复" not in snapshot.model_dump_json()

    oversized_trace_identity = job.model_copy(update={"job_id": "j" * 513})
    summary = await processor.process(oversized_trace_identity, execute_lifecycle=False)

    assert summary["candidate_count"] == 1


def _pipeline_candidate(
    *,
    tenant_id: str,
    user_id: str,
    suffix: str,
    evidence_text: str,
    operation: str = "add",
    value: str = "zh",
    temporal_scope: str = "long_term",
    certainty: str = "certain",
    target_memory_id: str | None = None,
) -> MemoryFormationCandidate:
    return MemoryFormationCandidate(
        candidate_id=f"candidate_pipeline_{suffix}",
        proposed_operation=operation,
        scope="user_preference",
        content=f"User response language preference: {value}",
        structured_value={"slot": "response_language", "value": value},
        semantic=MemoryCandidateSemantics(
            target="memory" if operation == "delete" else "assistant_response",
            slot="response_language",
            value=value,
            temporal_scope=temporal_scope,
            polarity="affirmed",
            certainty=certainty,
            change_intent={"add": "set", "update": "replace", "delete": "delete"}[operation],
        ),
        subject_id_hint=user_id,
        tenant_id_hint=tenant_id,
        memory_key_hint="response_language",
        target_memory_id=target_memory_id,
        confidence=0.99,
        evidence_refs=[
            MemoryEvidenceRef(
                turn_id=f"turn_{suffix}",
                role="user",
                quote=evidence_text,
            )
        ],
        reason="pipeline acceptance fixture",
    )


async def _process_pipeline_candidate(
    *,
    settings: Settings,
    formation: MemoryFormationTurnJobRepository,
    memories: MemoryItemRepository,
    service: MemoryService,
    tenant_id: str,
    user_id: str,
    suffix: str,
    evidence_text: str,
    candidate: MemoryFormationCandidate,
    verifier=None,
) -> dict:
    turn = _turn(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=suffix,
        content=evidence_text,
    )
    job = _job(
        tenant_id=tenant_id,
        user_id=user_id,
        suffix=suffix,
        turn_id=turn.turn_id,
    )
    await formation.append_turn(turn)
    await formation.add_job(job)
    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=memories,
        model=FakeConversationFormationModel(ConversationFormationResponse(candidates=[candidate])),
        policy=MemoryCandidatePolicy(
            settings=settings,
            repository=memories,
            verifier=verifier,
        ),
        lifecycle=service.lifecycle,
    )
    return await processor.process(job, execute_lifecycle=True)


@pytest.mark.asyncio
async def test_three_layer_processor_covers_semantic_lifecycle_acceptance_matrix() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    service = MemoryService(
        settings=settings,
        repository=memories,
        formation_repository=formation,
    )

    add_text = "以后请用中文回答"
    added = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_a",
        user_id="user_pipeline_a",
        suffix="pipeline_add_zh",
        evidence_text=add_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_a",
            user_id="user_pipeline_a",
            suffix="pipeline_add_zh",
            evidence_text=add_text,
        ),
    )
    assert added["operation_counts"] == {"add": 1}
    assert added["semantic_validation_counts"] == {"confirmed": 1}
    current_a = (
        await memories.list_active(
            tenant_id="tenant_pipeline_a", user_id="user_pipeline_a", limit=10
        )
    )[0]
    assert current_a.structured_value["value"] == "zh"
    assert current_a.current_revision_no == 1

    quoted_text = "请把‘以后请用英文回答’放进客户话术模板"
    quoted = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_a",
        user_id="user_pipeline_a",
        suffix="pipeline_quoted_template",
        evidence_text=quoted_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_a",
            user_id="user_pipeline_a",
            suffix="pipeline_quoted_template",
            evidence_text=quoted_text,
            operation="update",
            value="en",
        ),
    )
    assert quoted["operation_counts"] == {"pending": 1}
    assert quoted["semantic_verifier_counts"] == {"temporary_response_language_guard": 1}

    current_turn_text = "这次请用英文回答"
    current_turn = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_a",
        user_id="user_pipeline_a",
        suffix="pipeline_current_turn",
        evidence_text=current_turn_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_a",
            user_id="user_pipeline_a",
            suffix="pipeline_current_turn",
            evidence_text=current_turn_text,
            operation="update",
            value="en",
            temporal_scope="current_turn",
        ),
    )
    assert current_turn["operation_counts"] == {"noop": 1}
    assert current_turn["semantic_validation_counts"] == {"current_turn": 1}

    spanish_text = "A partir de ahora, responde en English."
    ambiguous = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_a",
        user_id="user_pipeline_a",
        suffix="pipeline_ambiguous_es",
        evidence_text=spanish_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_a",
            user_id="user_pipeline_a",
            suffix="pipeline_ambiguous_es",
            evidence_text=spanish_text,
            operation="update",
            value="en",
            certainty="uncertain",
        ),
    )
    assert ambiguous["operation_counts"] == {"pending": 1}
    assert ambiguous["semantic_validation_counts"] == {"pending": 1}
    assert ambiguous["semantic_verifier_counts"] == {"not_configured": 1}

    updated = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_a",
        user_id="user_pipeline_a",
        suffix="pipeline_update_es",
        evidence_text=spanish_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_a",
            user_id="user_pipeline_a",
            suffix="pipeline_update_es",
            evidence_text=spanish_text,
            operation="update",
            value="en",
        ),
    )
    assert updated["operation_counts"] == {"update": 1}
    current_a = (
        await memories.list_active(
            tenant_id="tenant_pipeline_a", user_id="user_pipeline_a", limit=10
        )
    )[0]
    assert current_a.structured_value["value"] == "en"
    assert current_a.current_revision_no == 2

    tenant_b_text = "以后请用中文回答"
    await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_b",
        user_id="user_pipeline_b",
        suffix="pipeline_tenant_b",
        evidence_text=tenant_b_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_b",
            user_id="user_pipeline_b",
            suffix="pipeline_tenant_b",
            evidence_text=tenant_b_text,
        ),
    )
    current_b = (
        await memories.list_active(
            tenant_id="tenant_pipeline_b", user_id="user_pipeline_b", limit=10
        )
    )[0]
    cross_tenant_text = "Forget the saved response-language preference."
    cross_tenant = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_a",
        user_id="user_pipeline_a",
        suffix="pipeline_cross_tenant_delete",
        evidence_text=cross_tenant_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_a",
            user_id="user_pipeline_a",
            suffix="pipeline_cross_tenant_delete",
            evidence_text=cross_tenant_text,
            operation="delete",
            value="en",
            target_memory_id=current_b.memory_id,
        ),
    )
    assert cross_tenant["operation_counts"] == {"reject": 1}
    assert (
        await memories.get_by_id(current_b.memory_id, tenant_id="tenant_pipeline_b")
    ).lifecycle_status == "active"

    authorized_text = "Forget my saved response-language preference."
    deleted = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_pipeline_a",
        user_id="user_pipeline_a",
        suffix="pipeline_authorized_delete",
        evidence_text=authorized_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_pipeline_a",
            user_id="user_pipeline_a",
            suffix="pipeline_authorized_delete",
            evidence_text=authorized_text,
            operation="delete",
            value="en",
            target_memory_id=current_a.memory_id,
        ),
    )
    assert deleted["operation_counts"] == {"delete": 1}
    assert (
        await memories.list_active(
            tenant_id="tenant_pipeline_a", user_id="user_pipeline_a", limit=10
        )
    ) == []
    assert (
        await memories.get_by_id(current_a.memory_id, tenant_id="tenant_pipeline_a")
    ).lifecycle_status == "deletion_pending"


class _ReadOnlyVerifier:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls = 0

    async def verify(self, *, candidate, turns):
        self.calls += 1
        assert candidate.semantic is not None
        assert turns
        return MemorySemanticVerification(
            verdict=self.verdict,
            confidence=0.99,
            reason_code="acceptance_verdict",
        )


class _CurrentStateRacingVerifier(_ReadOnlyVerifier):
    def __init__(self, repository: MemoryItemRepository) -> None:
        super().__init__("confirmed")
        self.repository = repository

    async def verify(self, *, candidate, turns):
        await self.repository.add(
            MemoryItem(
                memory_id="memory_concurrent_acceptance",
                memory_key=build_memory_key(
                    tenant_id="tenant_verifier_race",
                    user_id="user_verifier_race",
                    scope="user_preference",
                    hint="response_language",
                    structured={},
                ),
                scope="user_preference",
                subject_id="user_verifier_race",
                user_id="user_verifier_race",
                tenant_id="tenant_verifier_race",
                content="Concurrent English preference",
                structured_value={"slot": "response_language", "value": "en"},
                current_revision_id="revision_concurrent_acceptance",
            )
        )
        return await super().verify(candidate=candidate, turns=turns)


@pytest.mark.asyncio
async def test_processor_verifier_has_no_lifecycle_side_effect_and_rechecks_current_race() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    service = MemoryService(settings=settings, repository=memories)
    evidence_text = "以后请用中文回答"
    uncertain = _ReadOnlyVerifier("uncertain")
    pending = await _process_pipeline_candidate(
        settings=settings,
        formation=formation,
        memories=memories,
        service=service,
        tenant_id="tenant_verifier",
        user_id="user_verifier",
        suffix="pipeline_verifier_uncertain",
        evidence_text=evidence_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_verifier",
            user_id="user_verifier",
            suffix="pipeline_verifier_uncertain",
            evidence_text=evidence_text,
            certainty="uncertain",
        ),
        verifier=uncertain,
    )
    assert pending["operation_counts"] == {"pending": 1}
    assert pending["semantic_verifier_counts"] == {"uncertain": 1}
    assert uncertain.calls == 1
    assert (
        await memories.list_active(tenant_id="tenant_verifier", user_id="user_verifier", limit=10)
        == []
    )
    assert service.index_outbox.operations == {}

    race_formation = MemoryFormationTurnJobRepository()
    race_memories = MemoryItemRepository()
    race_service = MemoryService(settings=settings, repository=race_memories)
    racing = _CurrentStateRacingVerifier(race_memories)
    raced = await _process_pipeline_candidate(
        settings=settings,
        formation=race_formation,
        memories=race_memories,
        service=race_service,
        tenant_id="tenant_verifier_race",
        user_id="user_verifier_race",
        suffix="pipeline_verifier_race",
        evidence_text=evidence_text,
        candidate=_pipeline_candidate(
            tenant_id="tenant_verifier_race",
            user_id="user_verifier_race",
            suffix="pipeline_verifier_race",
            evidence_text=evidence_text,
            certainty="uncertain",
        ),
        verifier=racing,
    )
    assert raced["operation_counts"] == {"pending": 1}
    assert raced["semantic_validation_counts"] == {"pending": 1}
    assert raced["semantic_verifier_counts"] == {"confirmed": 1}
    assert racing.calls == 1
    assert len(race_memories.items) == 1
    assert race_service.index_outbox.operations == {}


@pytest.mark.asyncio
async def test_cross_tenant_formation_consolidation_debug_management_and_recall_isolation() -> None:
    settings = _settings()
    items = MemoryItemRepository()
    formation = MemoryFormationTurnJobRepository()
    service = MemoryService(
        settings=settings,
        repository=items,
        formation_repository=formation,
    )
    policy = MemoryCandidatePolicy(settings=settings, repository=items)
    trace_repository = MemoryFormationTraceRepository(
        formation_repository=formation,
        event_repository=items,
    )
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=service,
        formation_repository=formation,
        trace_repository=trace_repository,
    )
    content = "用户偏好简洁风格回答"

    first_a = await _form(
        formation=formation,
        policy=policy,
        service=service,
        tenant_id="tenant_a",
        user_id="user_a",
        suffix="a_primary",
        slot="response_style_primary",
        content=content,
    )
    first_b = await _form(
        formation=formation,
        policy=policy,
        service=service,
        tenant_id="tenant_b",
        user_id="user_b",
        suffix="b_primary",
        slot="response_style_primary",
        content=content,
    )
    assert first_a.item is not None and first_b.item is not None
    agent_subject = MemoryItem(
        memory_id="memory_a_agent_subject",
        memory_key="tenant:tenant_a:agent:acceptance_agent:preference:response_style_primary",
        scope="user_preference",
        subject_type="agent",
        subject_id="acceptance_agent",
        user_id="user_a",
        tenant_id="tenant_a",
        agent_id="acceptance_agent",
        content=content,
        structured_value={"slot": "response_style_primary", "value": content},
        current_revision_id="revision_a_agent_subject",
        current_revision_no=1,
        index_status="ready",
        source="acceptance-subject-fixture",
    )
    await items.add(agent_subject)

    rejected_subject = await policy.evaluate(
        job=_job(
            tenant_id="tenant_a",
            user_id="user_a",
            suffix="a_agent_subject_attempt",
            turn_id="turn_a_primary",
        ),
        candidate=_candidate(
            tenant_id="tenant_a",
            user_id="user_a",
            suffix="a_agent_subject_attempt",
            slot="response_style_primary",
            content=content,
            evidence_text="以后请使用简洁风格回答",
        ).model_copy(
            update={
                "subject_type": "agent",
                "subject_id_hint": "acceptance_agent",
            }
        ),
        turns=[
            _turn(
                tenant_id="tenant_a",
                user_id="user_a",
                suffix="a_primary",
                content="以后请使用简洁风格回答",
            )
        ],
    )
    assert rejected_subject.operation.reason_code == MemoryFormationReasonCode.IDENTITY_MISMATCH
    await items.add(
        first_a.item.model_copy(
            update={
                "memory_id": "memory_a_duplicate",
                "memory_key": "tenant:tenant_a:user:user_a:preference:response_style_duplicate",
                "candidate_hash": "sha256:acceptance-duplicate",
                "current_revision_id": "revision_a_duplicate",
                "formation_job_id": "job_consolidation_fixture",
            }
        )
    )
    await _drain_index(service)

    recall_a = await service.recall(
        MemoryRecallRequest(
            query="concise answers",
            user=UserContext(id="user_a", attributes={"tenant_id": "tenant_a"}),
            scopes=["user_preference"],
            max_items=10,
        )
    )
    recall_b = await service.recall(
        MemoryRecallRequest(
            query="concise answers",
            user=UserContext(id="user_b", attributes={"tenant_id": "tenant_b"}),
            scopes=["user_preference"],
            max_items=10,
        )
    )
    assert len(recall_a.context.items) == 2
    assert agent_subject.memory_id not in {item.memory_id for item in recall_a.context.items}
    assert [item.memory_id for item in recall_b.context.items] == [first_b.item.memory_id]
    recall_agent_subject = await service.recall(
        MemoryRecallRequest(
            query="concise answers",
            user=UserContext(id="user_a", attributes={"tenant_id": "tenant_a"}),
            scopes=["user_preference"],
            subject_type="agent",
            subject_id="acceptance_agent",
            agent_id="acceptance_agent",
            max_items=10,
        )
    )
    assert [item.memory_id for item in recall_agent_subject.context.items] == [
        agent_subject.memory_id
    ]

    debug_a = await observability.debug_state(tenant_id="tenant_a", user_id="user_a")
    debug_b = await observability.debug_state(tenant_id="tenant_b", user_id="user_b")
    assert len(debug_a.items) == 3 and all(item.tenant_id == "tenant_a" for item in debug_a.items)
    assert {(item.memory_id, item.subject_type, item.subject_id) for item in debug_a.items} >= {
        (agent_subject.memory_id, "agent", "acceptance_agent")
    }
    assert [item.memory_id for item in debug_b.items] == [first_b.item.memory_id]
    assert {trace.job.job_id for trace in debug_a.formation_traces} == {"job_a_primary"}
    assert {trace.job.job_id for trace in debug_b.formation_traces} == {"job_b_primary"}

    consolidation_job = MemoryFormationJob(
        job_id="job_consolidate_a",
        trigger="consolidation",
        mode="enforced",
        tenant_id="tenant_a",
        user_id="user_a",
        source_refs=["event_consolidate_a"],
        idempotency_key="acceptance:consolidate:tenant_a:user_a",
        model_version="acceptance-model-v1",
        prompt_version="acceptance-prompt-v1",
        policy_version="acceptance-policy-v1",
    )
    consolidated = await MemoryConsolidationService(
        policy=policy,
        lifecycle=service.lifecycle,
    ).run_partition(job=consolidation_job)
    assert consolidated
    await _drain_index(service)
    remaining_a = await items.list_active(
        tenant_id="tenant_a",
        user_id="user_a",
        subject_type="user",
        subject_id="user_a",
        scopes=["user_preference"],
        limit=10,
    )
    remaining_b = await items.list_active(
        tenant_id="tenant_b", user_id="user_b", scopes=["user_preference"], limit=10
    )
    assert len(remaining_a) == 1
    assert [item.memory_id for item in remaining_b] == [first_b.item.memory_id]
    assert await items.get_by_id(agent_subject.memory_id, tenant_id="tenant_a") == agent_subject

    management = MemoryManagementService(memory_service=service)
    with pytest.raises(MemoryManagementNotFound, match="target not found"):
        await management.request_delete(
            memory_id=remaining_a[0].memory_id,
            tenant_id="tenant_a",
            user_id="other_user",
            actor="other_user",
            reason="cross-user attempt",
            idempotency_key="cross-user-delete",
            expected_revision_id=remaining_a[0].current_revision_id,
        )
    with pytest.raises(MemoryManagementNotFound, match="target not found"):
        await management.request_delete(
            memory_id=agent_subject.memory_id,
            tenant_id="tenant_a",
            user_id="user_a",
            actor="user_a",
            reason="cross-subject attempt",
            idempotency_key="cross-subject-delete",
            expected_revision_id=agent_subject.current_revision_id,
        )
    deleted = await management.request_delete(
        memory_id=remaining_a[0].memory_id,
        tenant_id="tenant_a",
        user_id="user_a",
        actor="user_a",
        reason="acceptance cleanup",
        idempotency_key="owned-delete",
        expected_revision_id=remaining_a[0].current_revision_id,
    )
    assert deleted.status == "pending"
    await _drain_index(service)

    final_a = await service.recall(
        MemoryRecallRequest(
            query="concise answers",
            user=UserContext(id="user_a", attributes={"tenant_id": "tenant_a"}),
            scopes=["user_preference"],
            max_items=10,
        )
    )
    final_b = await service.recall(
        MemoryRecallRequest(
            query="concise answers",
            user=UserContext(id="user_b", attributes={"tenant_id": "tenant_b"}),
            scopes=["user_preference"],
            max_items=10,
        )
    )
    assert final_a.context.items == []
    assert [item.memory_id for item in final_b.context.items] == [first_b.item.memory_id]
    final_agent_subject = await service.recall(
        MemoryRecallRequest(
            query="concise answers",
            user=UserContext(id="user_a", attributes={"tenant_id": "tenant_a"}),
            scopes=["user_preference"],
            subject_type="agent",
            subject_id="acceptance_agent",
            agent_id="acceptance_agent",
            max_items=10,
        )
    )
    assert [item.memory_id for item in final_agent_subject.context.items] == [
        agent_subject.memory_id
    ]
