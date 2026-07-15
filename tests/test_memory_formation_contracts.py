from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryChangeIntent,
    MemoryDecisionStatus,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationJobStatus,
    MemoryFormationMode,
    MemoryFormationReasonCode,
    MemoryFormationTrace,
    MemoryFormationTrigger,
    MemoryFormationTurn,
    MemoryIndexOperation,
    MemoryIndexStatus,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryLifecycleStatus,
    MemoryOperation,
    MemoryPolarity,
    MemoryRevision,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemoryTemporalScope,
)


def test_formation_settings_defaults_and_overrides() -> None:
    defaults = Settings(storage_backend="memory")
    assert defaults.memory_formation_mode == "off"
    assert defaults.memory_formation_window_turns == 5
    assert defaults.memory_formation_idle_seconds == 30
    assert defaults.memory_formation_auto_threshold == 0.90
    assert defaults.memory_formation_pending_threshold == 0.70

    overridden = Settings(
        storage_backend="memory",
        memory_formation_mode="observe",
        memory_formation_window_turns=8,
        memory_formation_idle_seconds=45,
        memory_formation_model_timeout_seconds=10,
        memory_formation_lease_seconds=30,
    )
    assert overridden.memory_formation_mode == "observe"
    assert overridden.memory_formation_window_turns == 8


@pytest.mark.parametrize(
    "values",
    [
        {"memory_formation_pending_threshold": 0.9},
        {
            "memory_formation_model_timeout_seconds": 60,
            "memory_formation_lease_seconds": 60,
        },
        {
            "memory_formation_retry_base_seconds": 10,
            "memory_formation_retry_max_seconds": 5,
        },
        {"memory_formation_max_attempts": 0},
    ],
)
def test_formation_settings_reject_invalid_threshold_lease_and_retry(values) -> None:
    with pytest.raises(ValidationError):
        Settings(storage_backend="memory", **values)


def test_formation_contracts_are_strict_and_round_trip() -> None:
    turn = MemoryFormationTurn(
        turn_id="turn_1",
        request_id="req_1",
        session_id="session_1",
        user_id="user_1",
        tenant_id="tenant_1",
        result_status="completed",
        completed_at=datetime.now(UTC),
    )
    evidence = MemoryEvidenceRef(turn_id=turn.turn_id, role="user", quote="以后使用中文")
    candidate = MemoryFormationCandidate(
        proposed_operation=MemoryOperation.ADD,
        scope="user_preference",
        content="使用中文回复",
        semantic=MemoryCandidateSemantics(
            target=MemorySemanticTarget.ASSISTANT_RESPONSE,
            slot="response_language",
            value="zh",
            temporal_scope=MemoryTemporalScope.LONG_TERM,
            polarity=MemoryPolarity.AFFIRMED,
            certainty=MemorySemanticCertainty.CERTAIN,
            change_intent=MemoryChangeIntent.SET,
        ),
        confidence=0.95,
        evidence_refs=[evidence],
    )
    job = MemoryFormationJob(
        trigger=MemoryFormationTrigger.TURN_WINDOW,
        mode=MemoryFormationMode.OBSERVE,
        tenant_id=turn.tenant_id,
        user_id=turn.user_id,
        session_id=turn.session_id,
        first_turn_id=turn.turn_id,
        last_turn_id=turn.turn_id,
        idempotency_key="tenant_1:session_1:turn_1:policy-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    operation = MemoryLifecycleOperation(
        operation=MemoryOperation.ADD,
        decision_status=MemoryDecisionStatus.OBSERVED,
        reason_code=MemoryFormationReasonCode.ACCEPTED_NEW,
        tenant_id=turn.tenant_id,
        user_id=turn.user_id,
        subject_type="user",
        subject_id=turn.user_id,
        memory_key="tenant:tenant_1:user:user_1:preference:response_language",
        candidate_hash="sha256:candidate",
        formation_job_id=job.job_id,
    )
    trace = MemoryFormationTrace(
        job=job,
        turn_ids=[turn.turn_id],
        request_ids=[turn.request_id],
        operations=[operation],
        candidate_count=1,
        decision_counts={MemoryOperation.ADD.value: 1},
    )

    assert MemoryFormationTrace.model_validate_json(trace.model_dump_json()) == trace
    assert candidate.evidence_refs[0].role == "user"
    assert candidate.semantic.slot == "response_language"
    with pytest.raises(ValidationError):
        MemoryFormationJob.model_validate({**job.model_dump(), "unexpected": True})


@pytest.mark.parametrize(
    "semantic",
    [
        {
            "target": "assistant_response",
            "slot": "Response Language",
            "value": "zh",
            "temporal_scope": "long_term",
            "polarity": "affirmed",
            "certainty": "certain",
            "change_intent": "set",
        },
        {
            "target": "assistant_response",
            "slot": "response_language",
            "value": float("nan"),
            "temporal_scope": "long_term",
            "polarity": "affirmed",
            "certainty": "certain",
            "change_intent": "set",
        },
        {
            "target": "assistant_response",
            "slot": "response_language",
            "value": (1, 2),
            "temporal_scope": "long_term",
            "polarity": "affirmed",
            "certainty": "certain",
            "change_intent": "set",
        },
        {
            "target": "assistant_response",
            "slot": "response_language",
            "value": {1: "one"},
            "temporal_scope": "long_term",
            "polarity": "affirmed",
            "certainty": "certain",
            "change_intent": "set",
        },
        {
            "target": "assistant_response",
            "slot": "response_language",
            "value": {"nested": (1, 2)},
            "temporal_scope": "long_term",
            "polarity": "affirmed",
            "certainty": "certain",
            "change_intent": "set",
        },
    ],
)
def test_candidate_semantics_reject_invalid_slot_or_non_json_value(semantic) -> None:
    with pytest.raises(ValidationError):
        MemoryCandidateSemantics.model_validate(semantic)


def test_memory_item_lifecycle_fields_preserve_legacy_construction() -> None:
    item = MemoryItem(
        scope="stable_fact",
        subject_id="user_1",
        content="legacy memory",
    )
    assert item.lifecycle_status == MemoryLifecycleStatus.ACTIVE
    assert item.index_status is None
    assert item.memory_key is None

    governed = item.model_copy(
        update={
            "memory_key": "tenant:t1:user:u1:fact:locale",
            "current_revision_id": "mrev_1",
            "current_revision_no": 1,
            "index_status": MemoryIndexStatus.PENDING,
        }
    )
    assert MemoryItem.model_validate_json(governed.model_dump_json()) == governed
    assert MemoryFormationJobStatus.DEAD_LETTER.value == "dead_letter"


def test_revision_and_index_operation_round_trip() -> None:
    revision = MemoryRevision(
        memory_id="mem_1",
        revision_no=1,
        memory_key="tenant:t1:user:u1:preference:language",
        operation=MemoryOperation.ADD,
        content="中文",
        policy_version="policy-v1",
    )
    operation = MemoryIndexOperation(
        idempotency_key="mem_1:revision:1:add",
        operation=MemoryOperation.ADD,
        memory_id=revision.memory_id,
        revision_id=revision.revision_id,
        tenant_id="t1",
    )

    assert MemoryRevision.model_validate_json(revision.model_dump_json()) == revision
    assert MemoryIndexOperation.model_validate_json(operation.model_dump_json()) == operation


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (
            MemoryFormationCandidate,
            {
                "proposed_operation": "pending",
                "scope": "stable_fact",
                "content": "value",
                "confidence": 0.8,
                "evidence_refs": [{"role": "user", "turn_id": "turn_1"}],
            },
        ),
        (
            MemoryRevision,
            {
                "memory_id": "mem_1",
                "revision_no": 1,
                "memory_key": "key",
                "operation": "reject",
                "content": "value",
                "policy_version": "v1",
            },
        ),
        (
            MemoryIndexOperation,
            {
                "idempotency_key": "operation-1",
                "operation": "noop",
                "memory_id": "mem_1",
                "tenant_id": "t1",
            },
        ),
    ],
)
def test_stage_specific_operation_schemas_reject_policy_only_operations(schema, payload) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate(payload)
