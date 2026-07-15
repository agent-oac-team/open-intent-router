import inspect
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationTurn,
    MemoryItem,
    MemorySemanticVerification,
)
from app.services.memory_candidate_compatibility import adapt_structured_candidate
from app.services.memory_candidate_hard_rules import MemoryCandidateHardRules, build_memory_key
from app.services.memory_candidate_policy import MemoryCandidatePolicy
from app.services.memory_candidate_safety_filter import TemporaryLanguageSafetyFilter
from app.services.memory_candidate_semantics import (
    MemoryCandidateSemanticValidator,
    SemanticValidationStatus,
)


def _turn(text: str = "以后请用中文回答") -> MemoryFormationTurn:
    return MemoryFormationTurn(
        turn_id="turn_1",
        request_id="request_1",
        session_id="session_1",
        run_id="run_1",
        user_id="user_1",
        tenant_id="tenant_1",
        user_text=text,
        assistant_text="好的",
        result_status="completed",
        completed_at=datetime(2026, 7, 14, tzinfo=UTC),
    )


def _job(trigger: str = "idle", source_refs: list[str] | None = None) -> MemoryFormationJob:
    return MemoryFormationJob(
        job_id="job_layers",
        trigger=trigger,
        mode="observe",
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        source_refs=source_refs or (["event_1"] if trigger != "idle" else ["turn_1"]),
        idempotency_key="layers-key",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v2",
    )


def _semantics(**updates) -> MemoryCandidateSemantics:
    values = {
        "target": "assistant_response",
        "slot": "response_language",
        "value": "zh",
        "temporal_scope": "long_term",
        "polarity": "affirmed",
        "certainty": "certain",
        "change_intent": "set",
    }
    values.update(updates)
    return MemoryCandidateSemantics.model_validate(values)


def _candidate(**updates) -> MemoryFormationCandidate:
    values = {
        "candidate_id": "candidate_layers",
        "proposed_operation": "add",
        "scope": "user_preference",
        "content": "用户偏好中文回答",
        "structured_value": {"slot": "response_language", "value": "zh"},
        "semantic": _semantics(),
        "subject_id_hint": "user_1",
        "tenant_id_hint": "tenant_1",
        "memory_key_hint": "response_language",
        "confidence": 0.99,
        "evidence_refs": [
            MemoryEvidenceRef(turn_id="turn_1", role="user", quote="以后请用中文回答")
        ],
    }
    values.update(updates)
    return MemoryFormationCandidate.model_validate(values)


def _policy(repository, *, verifier=None) -> MemoryCandidatePolicy:
    return MemoryCandidatePolicy(
        settings=Settings(storage_backend="memory"),
        repository=repository,
        verifier=verifier,
    )


async def _add_current(repository: MemoryItemRepository, *, content: str = "旧偏好") -> MemoryItem:
    key = build_memory_key(
        tenant_id="tenant_1",
        user_id="user_1",
        scope="user_preference",
        hint="response_language",
        structured={},
    )
    item = MemoryItem(
        memory_id="memory_current",
        scope="user_preference",
        subject_id="user_1",
        user_id="user_1",
        tenant_id="tenant_1",
        content=content,
        structured_value={"slot": "response_language", "value": "en"},
        memory_key=key,
        current_revision_id="revision_current",
    )
    await repository.add(item)
    return item


async def test_missing_semantics_from_untrusted_conversation_stays_pending() -> None:
    candidate = _candidate().model_copy(update={"semantic": None})

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn()]
    )

    assert result.operation.operation == "pending"
    assert result.redacted_trace["semantic_verifier"] == "not_configured"
    assert result.operation.metadata == {
        "semantic_contract_version": "v1",
        "semantic_validation": "pending",
        "semantic_verifier": "not_configured",
    }
    assert "value" not in str(result.operation.metadata)


@pytest.mark.parametrize(
    "semantic_update",
    [
        {"target": "unknown"},
        {"slot": "tone"},
        {"value": "en"},
        {"temporal_scope": "unknown"},
        {"polarity": "unknown"},
        {"certainty": "uncertain"},
        {"change_intent": "replace"},
    ],
)
def test_semantic_validator_rejects_field_inconsistency_to_pending(semantic_update) -> None:
    candidate = _candidate(semantic=_semantics(**semantic_update))

    result = MemoryCandidateSemanticValidator().validate(candidate=candidate, current=None)

    assert result.status == SemanticValidationStatus.PENDING


def test_current_turn_semantics_are_deterministic_noop() -> None:
    candidate = _candidate(semantic=_semantics(temporal_scope="current_turn"))

    result = MemoryCandidateSemanticValidator().validate(candidate=candidate, current=None)

    assert result.status == SemanticValidationStatus.CURRENT_TURN


async def test_validated_semantic_slot_controls_ordinary_memory_key() -> None:
    candidate = _candidate(
        structured_value={"value": "zh"},
        memory_key_hint="general",
        semantic=_semantics(slot="response_language"),
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn()]
    )

    assert result.operation.operation == "add"
    assert result.operation.memory_key.endswith(":preference:response_language")


async def test_temporary_language_filter_can_only_downgrade() -> None:
    candidate = _candidate(
        evidence_refs=[
            MemoryEvidenceRef(
                turn_id="turn_1",
                role="user",
                quote="请把‘用英文回答’写进模板",
            )
        ]
    )
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=candidate,
        turns=[_turn("请把‘用英文回答’写进模板")],
    )

    assert result.operation.operation == "pending"
    assert result.operation.metadata["semantic_validation"] == "confirmed"
    assert result.redacted_trace["semantic_verifier"] == "temporary_response_language_guard"
    source = inspect.getsource(TemporaryLanguageSafetyFilter.evaluate)
    assert "MemoryOperation.ADD" not in source
    assert "MemoryOperation.UPDATE" not in source
    assert "MemoryOperation.DELETE" not in source


async def test_current_turn_and_pending_safety_filter_report_actual_semantic_outcomes() -> None:
    current_turn = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=_candidate(semantic=_semantics(temporal_scope="current_turn")),
        turns=[_turn()],
    )
    safety_pending = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=_candidate(
            semantic=_semantics(certainty="uncertain"),
            evidence_refs=[
                MemoryEvidenceRef(
                    turn_id="turn_1",
                    role="user",
                    quote="请把‘用英文回答’写进模板",
                )
            ],
        ),
        turns=[_turn("请把‘用英文回答’写进模板")],
    )

    assert current_turn.operation.metadata["semantic_validation"] == "current_turn"
    assert safety_pending.operation.metadata["semantic_validation"] == "pending"


class _Verifier:
    def __init__(self, response=None, *, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    async def verify(self, *, candidate, turns):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("confirmed", "add"), ("uncertain", "pending"), ("contradicted", "pending")],
)
async def test_verifier_outcomes_never_bypass_policy(verdict: str, expected: str) -> None:
    verifier = _Verifier(
        MemorySemanticVerification(
            verdict=verdict,
            confidence=0.99,
            reason_code="bounded_verdict",
        )
    )
    candidate = _candidate(semantic=_semantics(certainty="uncertain"))

    result = await _policy(MemoryItemRepository(), verifier=verifier).evaluate(
        job=_job(), candidate=candidate, turns=[_turn()]
    )

    assert result.operation.operation == expected
    assert result.redacted_trace["semantic_verifier"] == verdict
    assert verifier.calls == 1


@pytest.mark.parametrize("response", [{"verdict": "invalid"}, object()])
async def test_invalid_verifier_output_keeps_pending(response) -> None:
    result = await _policy(MemoryItemRepository(), verifier=_Verifier(response)).evaluate(
        job=_job(),
        candidate=_candidate(semantic=_semantics(certainty="uncertain")),
        turns=[_turn()],
    )

    assert result.operation.operation == "pending"
    assert result.redacted_trace["semantic_verifier"] == "error"


class _RacingVerifier:
    def __init__(self, repository: MemoryItemRepository) -> None:
        self.repository = repository

    async def verify(self, *, candidate, turns):
        await _add_current(self.repository, content="并发写入的中文偏好")
        return MemorySemanticVerification(
            verdict="confirmed",
            confidence=0.99,
            reason_code="confirmed_before_race_check",
        )


async def test_confirmed_verifier_reruns_current_state_before_accepting() -> None:
    repository = MemoryItemRepository()
    result = await _policy(repository, verifier=_RacingVerifier(repository)).evaluate(
        job=_job(),
        candidate=_candidate(semantic=_semantics(certainty="uncertain")),
        turns=[_turn()],
    )

    assert result.operation.operation == "pending"
    assert result.operation.memory_id == "memory_current"
    assert len(repository.items) == 1


async def test_hard_rules_reject_verifier_independent_identity_and_evidence() -> None:
    verifier = _Verifier(
        MemorySemanticVerification(
            verdict="confirmed",
            confidence=1.0,
            reason_code="cannot_override_hard_rules",
        )
    )
    result = await _policy(MemoryItemRepository(), verifier=verifier).evaluate(
        job=_job(),
        candidate=_candidate(tenant_id_hint="tenant_other"),
        turns=[_turn()],
    )

    assert result.operation.reason_code == "identity_mismatch"
    assert verifier.calls == 0


@pytest.mark.parametrize(
    ("scope", "target", "structured", "semantic_value"),
    [
        ("task_memory", "task", {"slot": "task_status", "status": "running"}, "running"),
        (
            "artifact_reference",
            "artifact",
            {"slot": "reference", "artifact_id": "artifact_1", "uri": "s3://bucket/a"},
            {"artifact_id": "artifact_1", "uri": "s3://bucket/a"},
        ),
        ("session_summary", "session", {"slot": "summary"}, "bounded summary"),
    ],
)
async def test_assistant_only_nonprofile_candidates_are_rejected(
    scope: str,
    target: str,
    structured: dict,
    semantic_value,
) -> None:
    candidate = _candidate(
        scope=scope,
        content="bounded summary" if scope == "session_summary" else "canonical projection",
        structured_value=structured,
        semantic=_semantics(
            target=target,
            slot=structured["slot"],
            value=semantic_value,
            temporal_scope="session" if scope == "session_summary" else "canonical",
        ),
        memory_key_hint=structured["slot"],
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="assistant", quote="好的")],
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn()]
    )

    assert result.operation.operation == "reject"
    assert result.operation.reason_code == "assistant_only_evidence"


@pytest.mark.parametrize(
    ("scope", "target", "structured", "semantic_value"),
    [
        ("task_memory", "task", {"slot": "task_status", "status": "running"}, "pending"),
        (
            "artifact_reference",
            "artifact",
            {"slot": "reference", "artifact_id": "artifact_1", "uri": "s3://bucket/a"},
            {"artifact_id": "artifact_other", "uri": "s3://bucket/a"},
        ),
        ("session_summary", "session", {"slot": "summary"}, "different summary"),
    ],
)
def test_nonprofile_semantic_value_must_match_structured_projection(
    scope: str,
    target: str,
    structured: dict,
    semantic_value,
) -> None:
    candidate = _candidate(
        scope=scope,
        content="bounded summary" if scope == "session_summary" else "canonical projection",
        structured_value=structured,
        semantic=_semantics(
            target=target,
            slot=structured["slot"],
            value=semantic_value,
            temporal_scope="session" if scope == "session_summary" else "canonical",
        ),
        memory_key_hint=structured["slot"],
    )

    result = MemoryCandidateSemanticValidator().validate(candidate=candidate, current=None)

    assert result.status == SemanticValidationStatus.PENDING


def test_structured_compatibility_adapter_is_field_only_and_trigger_bounded() -> None:
    legacy = _candidate().model_copy(update={"semantic": None})
    adapted = adapt_structured_candidate(legacy, trigger="manual")
    untrusted = adapt_structured_candidate(legacy, trigger="idle")

    assert adapted.semantic is not None
    assert adapted.semantic.slot == "response_language"
    assert adapted.semantic.value == "zh"
    assert untrusted.semantic is None
    assert "evidence_refs" not in inspect.getsource(adapt_structured_candidate)


def test_hard_rules_public_surface_has_no_lifecycle_or_provider_dependency() -> None:
    parameters = set(inspect.signature(MemoryCandidateHardRules).parameters)
    assert parameters == {"repository", "clock"}
    constructor = inspect.getsource(MemoryCandidateHardRules.__init__)
    assert "lifecycle" not in constructor
    assert "provider" not in constructor
