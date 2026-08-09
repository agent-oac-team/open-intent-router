from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.repositories.context_stores import MemoryItemRepository
from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryCandidateSemantics,
    MemoryChangeIntent,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationTurn,
    MemoryItem,
    MemoryPolarity,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemoryTemporalScope,
)
from app.schemas.plans import Plan, PlanStep
from app.services.memory_candidate_policy import (
    MemoryCandidatePolicy,
    build_candidate_hash,
    build_memory_key,
)
from app.services.structured_event_projector import StructuredEventProjector


def _job(*, trigger: str = "idle", source_refs: list[str] | None = None) -> MemoryFormationJob:
    return MemoryFormationJob(
        job_id="job_1",
        trigger=trigger,
        mode="observe",
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        first_turn_id="turn_1" if trigger != "structured_event" else None,
        last_turn_id="turn_1" if trigger != "structured_event" else None,
        source_refs=source_refs or (["event_1"] if trigger == "structured_event" else ["turn_1"]),
        idempotency_key="job-key",
        model_version="model-v1",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )


def _turn(
    *,
    used_memory_ids: list[str] | None = None,
    user_text: str = "以后请用中文回答",
    assistant_text: str = "好的，以后用中文回答。",
) -> MemoryFormationTurn:
    return MemoryFormationTurn(
        turn_id="turn_1",
        request_id="request_1",
        session_id="session_1",
        run_id="run_1",
        user_id="user_1",
        tenant_id="tenant_1",
        user_text=user_text,
        assistant_text=assistant_text,
        result_status="completed",
        used_memory_ids=used_memory_ids or [],
        completed_at=datetime(2026, 7, 13, tzinfo=UTC),
    )


def _candidate(**updates) -> MemoryFormationCandidate:
    values = {
        "candidate_id": "candidate_1",
        "proposed_operation": "add",
        "scope": "user_preference",
        "content": "用户偏好中文回答",
        "structured_value": {"slot": "response_language", "value": "zh"},
        "subject_type": "user",
        "subject_id_hint": "user_1",
        "tenant_id_hint": "tenant_1",
        "memory_key_hint": "response_language",
        "confidence": 0.95,
        "importance": 0.8,
        "sensitivity": "normal",
        "evidence_refs": [
            MemoryEvidenceRef(turn_id="turn_1", role="user", quote="以后请用中文回答")
        ],
        "reason": "future preference",
    }
    values.update(updates)
    if "semantic" not in updates:
        structured = values["structured_value"]
        operation = MemoryCandidateOperation(values["proposed_operation"])
        scope = str(values["scope"])
        semantic_value = structured.get("value", values["content"])
        if scope == "task_memory":
            semantic_value = structured.get("status")
        elif scope == "artifact_reference":
            semantic_value = {
                "artifact_id": structured.get("artifact_id"),
                "uri": structured.get("uri"),
            }
        values["semantic"] = MemoryCandidateSemantics(
            target={
                "user_preference": MemorySemanticTarget.ASSISTANT_RESPONSE,
                "stable_fact": MemorySemanticTarget.USER_PROFILE,
                "task_memory": MemorySemanticTarget.TASK,
                "artifact_reference": MemorySemanticTarget.ARTIFACT,
                "session_summary": MemorySemanticTarget.SESSION,
            }.get(scope, MemorySemanticTarget.UNKNOWN),
            slot=str(structured.get("slot") or values.get("memory_key_hint") or "general").rsplit(
                ":", 1
            )[-1],
            value=semantic_value,
            temporal_scope=MemoryTemporalScope(
                structured.get(
                    "temporal_scope",
                    MemoryTemporalScope.CANONICAL
                    if scope in {"task_memory", "artifact_reference"}
                    else MemoryTemporalScope.SESSION
                    if scope == "session_summary"
                    else MemoryTemporalScope.LONG_TERM,
                )
            ),
            polarity=MemoryPolarity.AFFIRMED,
            certainty=MemorySemanticCertainty.CERTAIN,
            change_intent={
                MemoryCandidateOperation.ADD: MemoryChangeIntent.SET,
                MemoryCandidateOperation.UPDATE: MemoryChangeIntent.REPLACE,
                MemoryCandidateOperation.DELETE: MemoryChangeIntent.DELETE,
                MemoryCandidateOperation.IGNORE: MemoryChangeIntent.NONE,
            }[operation],
        )
    return MemoryFormationCandidate.model_validate(values)


def _policy(repository: MemoryItemRepository) -> MemoryCandidatePolicy:
    return MemoryCandidatePolicy(settings=Settings(storage_backend="memory"), repository=repository)


def _uncertain_semantic(
    *,
    slot: str = "response_language",
    value="unknown",
    target: MemorySemanticTarget = MemorySemanticTarget.ASSISTANT_RESPONSE,
    change_intent: MemoryChangeIntent = MemoryChangeIntent.UNKNOWN,
    polarity: MemoryPolarity = MemoryPolarity.UNKNOWN,
) -> MemoryCandidateSemantics:
    return MemoryCandidateSemantics(
        target=target,
        slot=slot,
        value=value,
        temporal_scope=MemoryTemporalScope.UNKNOWN,
        polarity=polarity,
        certainty=MemorySemanticCertainty.UNCERTAIN,
        change_intent=change_intent,
    )


async def _current(repository: MemoryItemRepository, *, content: str = "用户偏好中文回答"):
    key = build_memory_key(
        tenant_id="tenant_1",
        user_id="user_1",
        scope="user_preference",
        hint="response_language",
        structured={},
    )
    item = MemoryItem(
        memory_id="memory_1",
        scope="user_preference",
        subject_id="user_1",
        user_id="user_1",
        tenant_id="tenant_1",
        content=content,
        structured_value={"slot": "response_language", "value": "zh"},
        memory_key=key,
        candidate_hash="old-hash",
        current_revision_id="revision_1",
        current_revision_no=1,
    )
    await repository.add(item)
    return item


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"tenant_id_hint": "tenant_2"}, "identity_mismatch"),
        ({"subject_id_hint": "user_2"}, "identity_mismatch"),
        ({"subject_type": "tenant"}, "identity_mismatch"),
        ({"scope": "unknown_scope"}, "invalid_scope"),
    ],
)
async def test_policy_reconstructs_trusted_identity_and_scope(updates, reason) -> None:
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=_candidate(**updates), turns=[_turn()]
    )

    assert result.operation.operation == "reject"
    assert result.operation.reason_code == reason
    assert result.operation.tenant_id == "tenant_1"
    assert result.operation.subject_id == "user_1"


async def test_policy_rejects_invalid_assistant_only_and_recalled_evidence() -> None:
    assistant = _candidate(
        evidence_refs=[
            MemoryEvidenceRef(turn_id="turn_1", role="assistant", quote="好的，以后用中文回答。")
        ]
    )
    policy = _policy(MemoryItemRepository())

    plain = await policy.evaluate(job=_job(), candidate=assistant, turns=[_turn()])
    recalled = await policy.evaluate(
        job=_job(), candidate=assistant, turns=[_turn(used_memory_ids=["memory_old"])]
    )
    invalid = await policy.evaluate(
        job=_job(),
        candidate=_candidate(evidence_refs=[MemoryEvidenceRef(turn_id="turn_2", role="user")]),
        turns=[_turn()],
    )

    assert plain.operation.reason_code == "assistant_only_evidence"
    assert recalled.operation.reason_code == "recalled_memory_repetition"
    assert invalid.operation.reason_code == "invalid_evidence"


@pytest.mark.parametrize(
    "content",
    [
        "password=super-secret",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "verification code: 123456",
        "Cookie: sessionid=abc123secret",
        "client_secret=client-secret-value",
        "sk-proj-abcdefghijklmnopqrstuv",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturevalue",
        "Jane email jane@example.com",
        "Call my coworker at +1 (415) 555-2671",
        "Alice home address is 123 Main Street",
        "My coworker has HIV",
        "-----BEGIN PRIVATE KEY-----",
        "SSN 123-45-6789",
        "card 4111111111111111",
        "身份证 11010519491231002X",
    ],
)
async def test_policy_rejects_sensitive_values_with_redacted_trace(content: str) -> None:
    candidate = _candidate(
        content=content,
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=content)],
    )
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=content)]
    )

    assert result.operation.reason_code == "sensitive_content"
    assert result.redacted_trace["content_redacted"] is True
    assert content not in str(result.redacted_trace)


async def test_policy_rejects_declared_third_party_private_data() -> None:
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=_candidate(
            structured_value={"privacy_subject": "third_party", "value": "private detail"}
        ),
        turns=[_turn()],
    )

    assert result.operation.reason_code == "sensitive_content"


@pytest.mark.parametrize("field", ["evidence", "reason", "hint"])
async def test_dlp_scans_all_model_control_fields_and_redacts_trace(field: str) -> None:
    secret = "password=super-secret"
    updates = {}
    user_text = "以后请用中文回答"
    if field == "evidence":
        quote = f"以后请用中文回答 {secret}"
        updates["evidence_refs"] = [MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)]
        user_text = quote
    elif field == "reason":
        updates["reason"] = secret
    else:
        updates["memory_key_hint"] = secret
    candidate = _candidate(**updates)

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=user_text)]
    )

    assert result.operation.reason_code == "sensitive_content"
    assert secret not in str(result.redacted_trace)
    assert result.redacted_trace["memory_key"].startswith("redacted:sha256:")


async def test_policy_thresholds_add_pending_and_reject() -> None:
    policy = _policy(MemoryItemRepository())
    added = await policy.evaluate(
        job=_job(), candidate=_candidate(confidence=0.90), turns=[_turn()]
    )
    pending = await policy.evaluate(
        job=_job(), candidate=_candidate(confidence=0.70), turns=[_turn()]
    )
    rejected = await policy.evaluate(
        job=_job(), candidate=_candidate(confidence=0.69), turns=[_turn()]
    )

    assert (added.operation.operation, added.operation.reason_code) == ("add", "accepted_new")
    assert pending.operation.reason_code == "confidence_pending"
    assert rejected.operation.reason_code == "confidence_low"


@pytest.mark.parametrize(
    ("quote", "content", "language"),
    [
        ("以后请用中文回答", "User prefers responses in Chinese.", "zh"),
        ("Désormais, réponds en français", "User prefers responses in French.", "fr"),
        (
            "A partir de ahora responde en español",
            "User prefers responses in Spanish.",
            "es",
        ),
        ("以后我想要中文回复", "User prefers responses in Chinese.", "zh"),
        ("以后我希望用中文回复", "User prefers responses in Chinese.", "zh"),
        ("记住我喜欢中文回答", "User prefers responses in Chinese.", "zh"),
        ("请一直用中文回答", "User prefers responses in Chinese.", "zh"),
        (
            "Going forward, I want Chinese replies",
            "User prefers responses in Chinese.",
            "zh",
        ),
        (
            "In the future, I request French replies",
            "User prefers responses in French.",
            "fr",
        ),
        (
            "From now on, please reply in Japanese",
            "User prefers responses in Japanese.",
            "ja",
        ),
        (
            "Remember that I prefer French replies",
            "User prefers responses in French.",
            "fr",
        ),
        ("Always reply in English", "User prefers responses in English.", "en"),
        (
            "Please always respond in English",
            "User prefers responses in English.",
            "en",
        ),
        ("I always want French replies", "User prefers responses in French.", "fr"),
        (
            "From now on, I'd like English replies and I'd like concise answers",
            "User prefers responses in English.",
            "en",
        ),
        (
            "Going forward, I'd prefer English replies; I'll appreciate it",
            "User prefers responses in English.",
            "en",
        ),
        ("记住我喜欢中文回答，并保存这份文档", "User prefers responses in Chinese.", "zh"),
        ("以后请用中文回答，另外把标题写入模板", "User prefers responses in Chinese.", "zh"),
        (
            "Remember that I prefer French replies and save the document",
            "User prefers responses in French.",
            "fr",
        ),
        (
            "Going forward, reply in English and copy the guide",
            "User prefers responses in English.",
            "en",
        ),
        (
            "保存以下内容：用英文回答。以后请用中文回答",
            "User prefers responses in Chinese.",
            "zh",
        ),
        (
            "Store this text: reply in English. Going forward, reply in French",
            "User prefers responses in French.",
            "fr",
        ),
        ("以后请用“中文”回答", "User prefers responses in Chinese.", "zh"),
        ("以后请用「中文」回答", "User prefers responses in Chinese.", "zh"),
        (
            'From now on, reply in "English"',
            "User prefers responses in English.",
            "en",
        ),
        (
            "Going forward, I'd prefer 'French' replies",
            "User prefers responses in French.",
            "fr",
        ),
        ("今后请给我法语回复", "User prefers responses in French.", "fr"),
        ("以后请用 zh-CN 回答", "User prefers responses in Chinese.", "zh-CN"),
        (
            "From now on, reply in en-US",
            "User prefers responses in English.",
            "en-US",
        ),
    ],
)
async def test_response_language_evidence_supports_cross_language_canonical_content(
    quote: str, content: str, language: str
) -> None:
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=_candidate(
            content=content,
            structured_value={
                "slot": "response_language",
                "value": language,
                "change_intent": "explicit_long_term",
            },
            evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
        ),
        turns=[_turn(user_text=quote)],
    )

    assert (result.operation.operation, result.operation.reason_code) == (
        "add",
        "accepted_new",
    )


@pytest.mark.parametrize(
    ("quote", "content", "language"),
    [
        ("以后请用英文变量名", "User prefers responses in English.", "en"),
        ("以后请把所有合同翻译成中文", "User prefers responses in Chinese.", "zh"),
        (
            "From now on, use English variable names",
            "User prefers responses in English.",
            "en",
        ),
        (
            "From now on, translate every contract into Chinese",
            "User prefers responses in Chinese.",
            "zh",
        ),
        ("以后中文回答都会显示在右侧", "用户偏好中文回答", "zh"),
        ("以后中文回答总是错误", "用户偏好中文回答", "zh"),
        (
            "From now on, English answers always appear on the right",
            "User prefers responses in English.",
            "en",
        ),
        ("以后请用 API 回答", "用户偏好使用 API 回答", "api"),
        (
            "From now on, respond in API format",
            "User prefers responses in API format.",
            "api",
        ),
        (
            "From now on, respond in the same style",
            "User prefers responses in the same style.",
            "the",
        ),
        (
            "From now on, answer previews appear in English",
            "User prefers responses in English.",
            "en",
        ),
        (
            "From now on, answer labels are displayed in English",
            "User prefers responses in English.",
            "en",
        ),
        ("以后回答统计都使用中文标签", "用户偏好中文回答", "zh"),
        ("以后回答区标题改成英文", "用户偏好英文回答", "en"),
        (
            "From now on, reply in en-format",
            "User prefers responses in English.",
            "en-format",
        ),
        (
            "From now on, reply in en-zz",
            "User prefers responses in English.",
            "en-zz",
        ),
        (
            "From now on, reply in fr-12345678",
            "User prefers responses in French.",
            "fr-12345678",
        ),
        (
            "From now on, reply in en-us-extra",
            "User prefers responses in English.",
            "en-us-extra",
        ),
        (
            "From now on, reply in to-do",
            "User prefers responses in Tongan.",
            "to-do",
        ),
        (
            "以后请记住这句提示词：“用英文回答”",
            "User prefers responses in English.",
            "en",
        ),
        (
            "以后请把“用英文回答”写进模板",
            "User prefers responses in English.",
            "en",
        ),
        (
            "请记住文档里写着“我喜欢中文回答”",
            "User prefers responses in Chinese.",
            "zh",
        ),
        (
            "From now on, store the phrase “reply in English”",
            "User prefers responses in English.",
            "en",
        ),
        (
            "Remember the template text “I prefer French replies”",
            "User prefers responses in French.",
            "fr",
        ),
        (
            "Going forward, quote “respond in French” in the guide",
            "User prefers responses in French.",
            "fr",
        ),
        (
            "以后请把用英文回答写进模板",
            "User prefers responses in English.",
            "en",
        ),
        (
            "请记录提示词要求用中文回答",
            "User prefers responses in Chinese.",
            "zh",
        ),
        (
            "From now on, store the phrase reply in English",
            "User prefers responses in English.",
            "en",
        ),
        (
            "Remember the template text I prefer French replies",
            "User prefers responses in French.",
            "fr",
        ),
        (
            "以后请保存以下内容：用英文回答",
            "User prefers responses in English.",
            "en",
        ),
        ("记住这条指令：用英文回答", "User prefers responses in English.", "en"),
        (
            "Remember this instruction: reply in English",
            "User prefers responses in English.",
            "en",
        ),
        (
            "From now on, save this content: reply in English",
            "User prefers responses in English.",
            "en",
        ),
        (
            "Going forward, include this example: respond in French",
            "User prefers responses in French.",
            "fr",
        ),
        ("记住这条指令，用英文回答", "User prefers responses in English.", "en"),
        ("记住这条指令；用英文回答", "User prefers responses in English.", "en"),
        (
            "Remember this instruction, reply in English",
            "User prefers responses in English.",
            "en",
        ),
        (
            "以后请记录以下规则，然后用中文回答",
            "User prefers responses in Chinese.",
            "zh",
        ),
        (
            "From now on, reply in en-us-extra",
            "User prefers responses in English.",
            "en",
        ),
        (
            "From now on, reply in en-US-style",
            "User prefers responses in English.",
            "en-US",
        ),
        (
            "From now on, reply in zh-cn-extra",
            "User prefers responses in Chinese.",
            "zh",
        ),
    ],
)
async def test_response_language_requires_an_answer_or_reply_target(
    quote: str, content: str, language: str
) -> None:
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=_candidate(
            content=content,
            structured_value={
                "slot": "response_language",
                "value": language,
                "change_intent": "explicit_long_term",
            },
            evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
        ),
        turns=[_turn(user_text=quote)],
    )

    assert result.operation.operation in {"reject", "pending"}


async def test_policy_same_value_override_update_and_ambiguous_conflict() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    policy = _policy(repository)
    same = await policy.evaluate(job=_job(), candidate=_candidate(), turns=[_turn()])
    override = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="update",
            content="用户偏好英文回答",
            structured_value={
                "slot": "response_language",
                "value": "en",
                "temporal_scope": "current_turn",
            },
            confidence=0.5,
            evidence_refs=[
                MemoryEvidenceRef(turn_id="turn_1", role="user", quote="这次请用英文回答")
            ],
        ),
        turns=[_turn(user_text="这次请用英文回答")],
    )
    updated = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="update",
            content="用户偏好英文回答",
            structured_value={
                "slot": "response_language",
                "value": "en",
                "change_intent": "explicit_long_term",
            },
            evidence_refs=[
                MemoryEvidenceRef(turn_id="turn_1", role="user", quote="以后请用英文回答")
            ],
        ),
        turns=[_turn(user_text="以后请用英文回答")],
    )
    ambiguous = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            content="用户偏好英文回答",
            structured_value={"slot": "response_language", "value": "en"},
            evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote="请用英文回答")],
        ),
        turns=[_turn(user_text="请用英文回答")],
    )

    assert same.operation.reason_code == "same_value"
    assert override.operation.reason_code == "current_turn_override"
    assert updated.operation.operation == "update"
    assert updated.operation.memory_id == "memory_1"
    assert ambiguous.operation.reason_code == "ambiguous_conflict"


async def test_delete_requires_user_evidence_unique_current_and_matching_target() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    policy = _policy(repository)
    authorized = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="delete",
            target_memory_id="memory_1",
            content="请删除中文回答偏好",
            structured_value={
                "slot": "response_language",
                "delete_intent": "explicit_user",
            },
            evidence_refs=[
                MemoryEvidenceRef(turn_id="turn_1", role="user", quote="请删除中文回答偏好")
            ],
        ),
        turns=[_turn(user_text="请删除中文回答偏好")],
    )
    missing = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=_candidate(proposed_operation="delete"), turns=[_turn()]
    )
    mismatched = await policy.evaluate(
        job=_job(),
        candidate=_candidate(proposed_operation="delete", target_memory_id="memory_2"),
        turns=[_turn()],
    )

    assert authorized.operation.reason_code == "authorized_delete"
    assert missing.operation.reason_code == "ambiguous_delete"
    assert mismatched.operation.reason_code == "identity_mismatch"


async def test_delete_same_value_is_not_swallowed_and_ttl_delete_requires_sweeper() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    policy = _policy(repository)
    same_value_delete = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="delete",
            target_memory_id="memory_1",
            structured_value={
                "slot": "response_language",
                "value": "zh",
                "delete_intent": "explicit_user",
            },
            evidence_refs=[
                MemoryEvidenceRef(turn_id="turn_1", role="user", quote="请删除中文回答偏好")
            ],
        ),
        turns=[_turn(user_text="请删除中文回答偏好")],
    )
    ttl_candidate = _candidate(
        proposed_operation="delete",
        structured_value={"lifecycle_reason": "ttl_expired"},
        evidence_refs=[MemoryEvidenceRef(event_id="ttl_event_1", role="canonical")],
    )
    repository.items["memory_1"] = repository.items["memory_1"].model_copy(
        update={"ttl_expires_at": datetime(2020, 1, 1, tzinfo=UTC)}
    )
    ttl = await policy.evaluate(
        job=_job(trigger="sweeper", source_refs=["ttl_event_1"]),
        candidate=ttl_candidate,
        turns=[],
    )

    assert same_value_delete.operation.reason_code == "authorized_delete"
    assert ttl.operation.reason_code == "ttl_expired"
    assert ttl.operation.operation == "delete"


async def test_delete_without_explicit_marker_and_future_ttl_are_not_authorized() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    policy = MemoryCandidatePolicy(
        settings=Settings(storage_backend="memory"),
        repository=repository,
        clock=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )
    vague = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="delete",
            target_memory_id="memory_1",
            semantic=_uncertain_semantic(change_intent=MemoryChangeIntent.DELETE),
        ),
        turns=[_turn()],
    )
    repository.items["memory_1"] = repository.items["memory_1"].model_copy(
        update={"ttl_expires_at": datetime(2099, 1, 1, tzinfo=UTC)}
    )
    ttl = await policy.evaluate(
        job=_job(trigger="sweeper", source_refs=["ttl_event_1"]),
        candidate=_candidate(
            proposed_operation="delete",
            structured_value={"lifecycle_reason": "ttl_expired"},
            evidence_refs=[MemoryEvidenceRef(event_id="ttl_event_1", role="canonical")],
        ),
        turns=[],
    )

    assert vague.operation.reason_code == "ambiguous_delete"
    assert ttl.operation.reason_code == "invalid_evidence"


async def test_ttl_comparison_normalizes_naive_database_timestamp() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    repository.items["memory_1"] = repository.items["memory_1"].model_copy(
        update={"ttl_expires_at": datetime(2020, 1, 1)}
    )
    policy = MemoryCandidatePolicy(
        settings=Settings(storage_backend="memory"),
        repository=repository,
        clock=lambda: datetime(2026, 7, 13, tzinfo=UTC),
    )

    result = await policy.evaluate(
        job=_job(trigger="sweeper", source_refs=["ttl_event_1"]),
        candidate=_candidate(
            proposed_operation="delete",
            structured_value={"lifecycle_reason": "ttl_expired"},
            evidence_refs=[MemoryEvidenceRef(event_id="ttl_event_1", role="canonical")],
        ),
        turns=[],
    )

    assert result.operation.reason_code == "ttl_expired"


async def test_structured_canonical_evidence_and_replay_are_deterministic() -> None:
    candidate = _candidate(
        scope="task_memory",
        structured_value={
            "object_type": "plan",
            "plan_id": "plan_1",
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "status": "pending",
        },
        memory_key_hint="tenant:tenant_1:user:user_1:plan:plan_1:task_status",
        evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
    )
    policy = _policy(MemoryItemRepository())
    first = await policy.evaluate(
        job=_job(trigger="structured_event"), candidate=candidate, turns=[]
    )
    replay = await policy.evaluate(
        job=_job(trigger="structured_event"), candidate=candidate, turns=[]
    )

    assert first.operation.operation == "add"
    assert first.operation.operation_id == replay.operation.operation_id
    assert first.operation.candidate_hash == replay.operation.candidate_hash


async def test_structured_canonical_change_authoritatively_updates_current() -> None:
    repository = MemoryItemRepository()
    key = "tenant:tenant_1:user:user_1:plan:plan_1:task_status"
    await repository.add(
        MemoryItem(
            memory_id="plan_memory",
            scope="task_memory",
            subject_id="user_1",
            user_id="user_1",
            tenant_id="tenant_1",
            content="Plan plan_1 status=pending",
            structured_value={"status": "pending"},
            memory_key=key,
            current_revision_id="revision_1",
        )
    )
    candidate = _candidate(
        proposed_operation="update",
        scope="task_memory",
        content="Plan plan_1 status=running",
        structured_value={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "object_type": "plan",
            "plan_id": "plan_1",
            "status": "running",
        },
        memory_key_hint=key,
        evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
    )

    result = await _policy(repository).evaluate(
        job=_job(trigger="structured_event"), candidate=candidate, turns=[]
    )

    assert result.operation.operation == "update"
    assert result.operation.reason_code == "accepted_update"


async def test_real_plan_projection_passes_dlp_and_updates_expired_canonical_current() -> None:
    plan = Plan(
        plan_id="plan_1",
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        status="running",
        current_step_id="step_1",
        steps=[
            PlanStep(
                step_id="step_1",
                agent_id="agent_1",
                description="Write the quarterly report",
                status="running",
            )
        ],
    )
    projection = StructuredEventProjector().project_plan(
        plan,
        event_type="update",
        event_id="event_1",
        source_version="2",
        last_activity_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    repository = MemoryItemRepository()
    await repository.add(
        MemoryItem(
            memory_id="plan_memory",
            scope="task_memory",
            subject_id="user_1",
            user_id="user_1",
            tenant_id="tenant_1",
            content="Plan plan_1 status=pending",
            structured_value={"object_type": "plan", "plan_id": "plan_1", "status": "pending"},
            memory_key=projection.candidates[0].memory_key_hint,
            current_revision_id="revision_1",
            ttl_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )

    result = await _policy(repository).evaluate(
        job=_job(trigger="structured_event"),
        candidate=projection.candidates[0],
        turns=[],
    )

    assert result.operation.operation == "update"
    assert result.operation.memory_id == "plan_memory"
    assert result.operation.reason_code == "accepted_update"

    ttl_candidate = projection.candidates[0].model_copy(
        update={
            "proposed_operation": MemoryCandidateOperation.DELETE,
            "structured_value": {
                **projection.candidates[0].structured_value,
                "lifecycle_reason": "ttl_expired",
            },
            "evidence_refs": [MemoryEvidenceRef(event_id="ttl_event_1", role="canonical")],
        }
    )
    ttl = await _policy(repository).evaluate(
        job=_job(trigger="sweeper", source_refs=["ttl_event_1"]),
        candidate=ttl_candidate,
        turns=[],
    )

    assert ttl.operation.operation == "delete"
    assert ttl.operation.memory_id == "plan_memory"


@pytest.mark.parametrize(
    "description",
    ["Integrate partner API", "Build employee dashboard", "Send report to boss"],
)
async def test_business_relationship_words_do_not_block_canonical_plan(
    description: str,
) -> None:
    plan = Plan(
        plan_id="plan_business",
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        status="pending",
        steps=[
            PlanStep(
                step_id="step_1",
                agent_id="agent_1",
                description=description,
            )
        ],
    )
    projection = StructuredEventProjector().project_plan(
        plan,
        event_type="create",
        event_id="event_1",
        source_version="1",
        last_activity_at=datetime(2026, 7, 13, tzinfo=UTC),
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(trigger="structured_event"),
        candidate=projection.candidates[0],
        turns=[],
    )

    assert result.operation.operation == "add"


async def test_policy_never_reads_similar_current_from_another_tenant() -> None:
    repository = MemoryItemRepository()
    key = build_memory_key(
        tenant_id="tenant_1",
        user_id="user_1",
        scope="user_preference",
        hint="response_language",
        structured={},
    )
    await repository.add(
        MemoryItem(
            memory_id="other_tenant_memory",
            scope="user_preference",
            subject_id="user_1",
            user_id="user_1",
            tenant_id="tenant_2",
            content="用户偏好中文回答",
            memory_key=key,
        )
    )

    result = await _policy(repository).evaluate(job=_job(), candidate=_candidate(), turns=[_turn()])

    assert result.operation.operation == "add"
    assert result.operation.memory_id is None


async def test_policy_current_lookup_requires_exact_user_id() -> None:
    repository = MemoryItemRepository()
    key = build_memory_key(
        tenant_id="tenant_1",
        user_id="user_1",
        scope="user_preference",
        hint="response_language",
        structured={},
    )
    await repository.add(
        MemoryItem(
            memory_id="other_user_memory",
            scope="user_preference",
            subject_id="user_1",
            user_id="user_2",
            tenant_id="tenant_1",
            content="用户偏好中文回答",
            memory_key=key,
        )
    )

    result = await _policy(repository).evaluate(job=_job(), candidate=_candidate(), turns=[_turn()])

    assert result.operation.operation == "add"
    assert result.operation.memory_id is None


async def test_policy_rejects_cross_owner_turn_and_unrelated_recalled_evidence() -> None:
    cross_owner = _turn().model_copy(update={"tenant_id": "tenant_2", "user_id": "user_2"})
    cross = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=_candidate(), turns=[cross_owner]
    )
    unrelated_candidate = _candidate(
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote="今天天气怎么样")]
    )
    unrelated = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=unrelated_candidate,
        turns=[_turn(user_text="今天天气怎么样", used_memory_ids=["memory_old"])],
    )

    assert cross.operation.reason_code == "identity_mismatch"
    assert unrelated.operation.operation == "pending"


@pytest.mark.parametrize(
    ("quote", "content"),
    [
        ("住在上海的朋友推荐了这家店", "用户住在上海"),
        ("My friend lives in Paris", "User lives in Paris"),
    ],
)
async def test_third_party_statement_cannot_be_promoted_to_user_stable_fact(
    quote: str, content: str
) -> None:
    candidate = _candidate(
        scope="stable_fact",
        content=content,
        structured_value={"slot": "home_city", "privacy_subject": "third_party"},
        memory_key_hint="home_city",
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation == "reject"


@pytest.mark.parametrize(
    ("quote", "content"),
    [("我住在上海", "用户住在上海"), ("I live in Paris", "User lives in Paris")],
)
async def test_first_person_stable_fact_can_be_added(quote: str, content: str) -> None:
    candidate = _candidate(
        scope="stable_fact",
        content=content,
        structured_value={"slot": "home_city"},
        memory_key_hint="home_city",
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation == "add"


@pytest.mark.parametrize(
    ("quote", "content"),
    [
        ("I do not live in Paris", "User lives in Paris"),
        ("我不住在上海", "用户住在上海"),
        ("Do I live in Paris?", "User lives in Paris"),
        ("我住在上海吗？", "用户住在上海"),
        ("I might live in Paris next year", "User lives in Paris"),
        ("I wonder whether I live in Paris", "User lives in Paris"),
        ("I am unsure whether I live in Paris", "User lives in Paris"),
        ("I used to live in Paris", "User lives in Paris"),
        ("I lived in Paris last year", "User lives in Paris"),
        ("I will live in Paris next month", "User lives in Paris"),
        ("我以前住在上海", "用户住在上海"),
    ],
)
async def test_nonassertive_or_polarity_changed_stable_fact_is_rejected(
    quote: str, content: str
) -> None:
    candidate = _candidate(
        scope="stable_fact",
        content=content,
        structured_value={"slot": "home_city"},
        semantic=_uncertain_semantic(
            slot="home_city",
            value=content,
            target=MemorySemanticTarget.USER_PROFILE,
        ),
        memory_key_hint="home_city",
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation == "pending"


@pytest.mark.parametrize(
    ("quote", "content"),
    [
        ("I work in Paris", "User lives in Paris"),
        ("I vacation in Paris", "User lives in Paris"),
        ("We serve customers in Paris", "User lives in Paris"),
        ("我在上海工作", "用户住在上海"),
    ],
)
async def test_stable_fact_predicate_must_match_evidence(quote: str, content: str) -> None:
    candidate = _candidate(
        scope="stable_fact",
        content=content,
        structured_value={"slot": "home_city"},
        semantic=MemoryCandidateSemantics(
            target=MemorySemanticTarget.USER_PROFILE,
            slot="work_city",
            value=content,
            temporal_scope=MemoryTemporalScope.LONG_TERM,
            polarity=MemoryPolarity.AFFIRMED,
            certainty=MemorySemanticCertainty.CERTAIN,
            change_intent=MemoryChangeIntent.SET,
        ),
        memory_key_hint="home_city",
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation == "pending"


async def test_response_language_uses_instruction_target_not_translation_source() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    quote = "以后把英文回答翻译成中文"
    candidate = _candidate(
        proposed_operation="update",
        content="用户偏好英文回答",
        structured_value={
            "slot": "response_language",
            "value": "en",
            "change_intent": "explicit_long_term",
        },
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
    )

    result = await _policy(repository).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation in {"reject", "pending"}


@pytest.mark.parametrize(
    ("quote", "content"),
    [
        ("中文很有趣", "用户偏好中文回答"),
        ("English is interesting", "User prefers English answers"),
        ("这个用户问天气", "用户偏好中文回答"),
        ("用户喜欢中文电影", "用户偏好中文回答"),
        ("User likes English movies", "User prefers English answers"),
    ],
)
async def test_single_generic_overlap_does_not_prove_memory_change(
    quote: str, content: str
) -> None:
    candidate = _candidate(
        proposed_operation="update",
        content=content,
        structured_value={
            "slot": "response_language",
            "value": "new",
            "change_intent": "explicit_long_term",
        },
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
    )

    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation in {"reject", "pending"}


async def test_mentions_without_lifecycle_language_cannot_update_or_delete() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    policy = _policy(repository)
    update_quote = "English answers are interesting"
    update = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="update",
            content="User prefers English answers",
            structured_value={
                "slot": "response_language",
                "value": "en",
                "change_intent": "explicit_long_term",
            },
            evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=update_quote)],
            semantic=_uncertain_semantic(value="en"),
        ),
        turns=[_turn(user_text=update_quote)],
    )
    delete_quote = "中文回答很有趣"
    delete = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="delete",
            content="用户偏好中文回答",
            target_memory_id="memory_1",
            structured_value={
                "slot": "response_language",
                "value": "zh",
                "delete_intent": "explicit_user",
            },
            evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=delete_quote)],
            semantic=_uncertain_semantic(
                value="zh",
                change_intent=MemoryChangeIntent.DELETE,
            ),
        ),
        turns=[_turn(user_text=delete_quote)],
    )

    assert update.operation.operation in {"pending", "reject"}
    assert delete.operation.operation in {"pending", "reject"}


@pytest.mark.parametrize(
    ("quote", "operation", "content"),
    [
        ("English answers are always wrong", "update", "User prefers English answers"),
        (
            "Please remove the Chinese answers section from this report",
            "delete",
            "User prefers Chinese answers",
        ),
        ("删除中文回答这一段内容", "delete", "用户偏好中文回答"),
    ],
)
async def test_document_or_complaint_language_cannot_authorize_lifecycle_change(
    quote: str, operation: str, content: str
) -> None:
    repository = MemoryItemRepository()
    await _current(repository, content=content)
    structured = {"slot": "response_language", "value": "new"}
    if operation == "update":
        structured["change_intent"] = "explicit_long_term"
    else:
        structured["delete_intent"] = "explicit_user"
    candidate = _candidate(
        proposed_operation=operation,
        content=content,
        target_memory_id="memory_1" if operation == "delete" else None,
        structured_value=structured,
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
        semantic=_uncertain_semantic(
            value=structured.get("value", content),
            change_intent=(
                MemoryChangeIntent.REPLACE if operation == "update" else MemoryChangeIntent.DELETE
            ),
        ),
    )

    result = await _policy(repository).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation in {"reject", "pending", "noop"}


@pytest.mark.parametrize(
    "quote",
    [
        "Do not delete my saved Chinese answers preference",
        "不要删除我已保存的中文回答偏好",
        "Should I delete my saved Chinese answers preference?",
    ],
)
async def test_negated_or_question_delete_is_never_authorized(quote: str) -> None:
    repository = MemoryItemRepository()
    await _current(repository, content="User prefers Chinese answers")
    candidate = _candidate(
        proposed_operation="delete",
        content="Delete my saved Chinese answers preference",
        target_memory_id="memory_1",
        structured_value={"slot": "response_language", "delete_intent": "explicit_user"},
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
        semantic=_uncertain_semantic(
            value="zh",
            change_intent=MemoryChangeIntent.DELETE,
        ),
    )

    result = await _policy(repository).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation in {"pending", "reject"}


@pytest.mark.parametrize("quote", ["From now on, do not answer in English", "以后不要用英文回答"])
async def test_long_term_update_cannot_reverse_evidence_polarity(quote: str) -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    candidate = _candidate(
        proposed_operation="update",
        content="User prefers English answers",
        structured_value={
            "slot": "response_language",
            "value": "en",
            "change_intent": "explicit_long_term",
        },
        evidence_refs=[MemoryEvidenceRef(turn_id="turn_1", role="user", quote=quote)],
        semantic=MemoryCandidateSemantics(
            target=MemorySemanticTarget.ASSISTANT_RESPONSE,
            slot="response_language",
            value="en",
            temporal_scope=MemoryTemporalScope.LONG_TERM,
            polarity=MemoryPolarity.NEGATED,
            certainty=MemorySemanticCertainty.CERTAIN,
            change_intent=MemoryChangeIntent.REPLACE,
        ),
    )

    result = await _policy(repository).evaluate(
        job=_job(), candidate=candidate, turns=[_turn(user_text=quote)]
    )

    assert result.operation.operation in {"pending", "reject"}


async def test_redacted_trace_covers_every_policy_operation() -> None:
    empty_policy = _policy(MemoryItemRepository())
    added = await empty_policy.evaluate(job=_job(), candidate=_candidate(), turns=[_turn()])
    rejected = await empty_policy.evaluate(
        job=_job(), candidate=_candidate(confidence=0.1), turns=[_turn()]
    )
    pending = await empty_policy.evaluate(
        job=_job(), candidate=_candidate(confidence=0.8), turns=[_turn()]
    )
    repository = MemoryItemRepository()
    await _current(repository)
    policy = _policy(repository)
    noop = await policy.evaluate(job=_job(), candidate=_candidate(), turns=[_turn()])
    updated = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="update",
            content="用户偏好英文回答",
            structured_value={
                "slot": "response_language",
                "value": "en",
                "change_intent": "explicit_long_term",
            },
            evidence_refs=[
                MemoryEvidenceRef(turn_id="turn_1", role="user", quote="以后请用英文回答")
            ],
        ),
        turns=[_turn(user_text="以后请用英文回答")],
    )
    deleted = await policy.evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="delete",
            target_memory_id="memory_1",
            content="请删除中文回答偏好",
            structured_value={
                "slot": "response_language",
                "delete_intent": "explicit_user",
            },
            evidence_refs=[
                MemoryEvidenceRef(turn_id="turn_1", role="user", quote="请删除中文回答偏好")
            ],
        ),
        turns=[_turn(user_text="请删除中文回答偏好")],
    )

    results = [added, updated, deleted, noop, rejected, pending]
    assert {result.operation.operation.value for result in results} == {
        "add",
        "update",
        "delete",
        "noop",
        "reject",
        "pending",
    }
    for result in results:
        assert result.redacted_trace["reason_code"] == result.operation.reason_code.value
        assert result.candidate.content not in str(result.redacted_trace)


def test_memory_key_and_candidate_hash_are_deterministic_and_bounded() -> None:
    first = build_memory_key(
        tenant_id="租" * 128,
        user_id="户" * 128,
        scope="stable_fact",
        hint="时区偏好",
        structured={},
    )
    second = build_memory_key(
        tenant_id="租" * 128,
        user_id="户" * 128,
        scope="stable_fact",
        hint="时区偏好",
        structured={},
    )

    assert first == second
    assert len(first) <= 512
    assert build_candidate_hash(_candidate()) == build_candidate_hash(_candidate())


def test_untrusted_full_memory_key_hint_is_rebuilt_but_structured_hint_is_preserved() -> None:
    forged = "tenant:tenant_1:user:user_1:plan:plan_1:task_status"
    natural = build_memory_key(
        tenant_id="tenant_1",
        user_id="user_1",
        scope="user_preference",
        hint=forged,
        structured={},
    )
    structured = build_memory_key(
        tenant_id="tenant_1",
        user_id="user_1",
        scope="task_memory",
        hint=forged,
        structured={"object_type": "plan", "plan_id": "plan_1"},
        allow_canonical_hint=True,
    )

    assert natural == "tenant:tenant_1:user:user_1:preference:task_status"
    assert structured == forged


def test_key_segments_prevent_owner_collision_and_reject_forged_structured_owner() -> None:
    first = build_memory_key(
        tenant_id="a:user:b", user_id="c", scope="stable_fact", hint="locale", structured={}
    )
    second = build_memory_key(
        tenant_id="a", user_id="b:user:c", scope="stable_fact", hint="locale", structured={}
    )
    forged = build_memory_key(
        tenant_id="tenant_1",
        user_id="user_1",
        scope="task_memory",
        hint="tenant:tenant_X:user:user_X:plan:plan_1:task_status",
        structured={},
        allow_canonical_hint=True,
    )

    assert first != second
    assert forged.startswith("tenant:tenant_1:user:user_1:")
    assert "tenant_X" not in forged


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(
            scope="task_memory",
            memory_key_hint="tenant:tenant_1:user:user_1:plan:plan_A:task_status",
            structured_value={"object_type": "plan", "plan_id": "plan_B"},
            evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
        ),
        _candidate(
            scope="stable_fact",
            evidence_refs=[MemoryEvidenceRef(event_id="event_1", role="canonical")],
        ),
    ],
)
async def test_structured_event_rejects_object_key_mismatch_and_noncanonical_scope(
    candidate: MemoryFormationCandidate,
) -> None:
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(trigger="structured_event"), candidate=candidate, turns=[]
    )

    assert result.operation.operation == "reject"


async def test_explicit_delete_of_sensitive_current_is_allowed_and_trace_is_redacted() -> None:
    repository = MemoryItemRepository()
    current = await _current(repository, content="SSN 123-45-6789")
    candidate = _candidate(
        proposed_operation="delete",
        content="Delete my saved memory SSN 123-45-6789",
        target_memory_id=current.memory_id,
        structured_value={"slot": "response_language", "delete_intent": "explicit_user"},
        evidence_refs=[
            MemoryEvidenceRef(
                turn_id="turn_1",
                role="user",
                quote="Delete my saved memory SSN 123-45-6789",
            )
        ],
    )

    result = await _policy(repository).evaluate(
        job=_job(),
        candidate=candidate,
        turns=[_turn(user_text="Delete my saved memory SSN 123-45-6789")],
    )

    assert result.operation.reason_code == "authorized_delete"
    assert result.operation.memory_key == current.memory_key
    assert result.redacted_trace["content_redacted"] is True
    assert result.redacted_trace["memory_key"].startswith("redacted:sha256:")
    assert "123-45-6789" not in str(result.redacted_trace)


def test_candidate_hash_normalizes_evidence_order() -> None:
    first = _candidate(
        evidence_refs=[
            MemoryEvidenceRef(turn_id="turn_1", role="user", quote="a"),
            MemoryEvidenceRef(turn_id="turn_2", role="user", quote="b"),
        ]
    )
    second = first.model_copy(update={"evidence_refs": list(reversed(first.evidence_refs))})

    assert build_candidate_hash(first) == build_candidate_hash(second)


async def test_control_fields_do_not_change_same_value_and_slot_overrides_hint() -> None:
    repository = MemoryItemRepository()
    await _current(repository)
    same = await _policy(repository).evaluate(
        job=_job(),
        candidate=_candidate(
            proposed_operation="update",
            structured_value={
                "slot": "response_language",
                "value": "zh",
                "change_intent": "explicit_long_term",
            },
        ),
        turns=[_turn()],
    )
    empty_policy = _policy(MemoryItemRepository())
    slot_a = await empty_policy.evaluate(
        job=_job(), candidate=_candidate(memory_key_hint="slot_a"), turns=[_turn()]
    )
    slot_b = await empty_policy.evaluate(
        job=_job(), candidate=_candidate(memory_key_hint="slot_b"), turns=[_turn()]
    )

    assert same.operation.reason_code == "same_value"
    assert slot_a.operation.candidate_hash == slot_b.operation.candidate_hash
    assert slot_a.operation.memory_key == slot_b.operation.memory_key
    assert slot_a.operation.operation_id == slot_b.operation.operation_id


async def test_current_turn_override_never_adds_without_current() -> None:
    result = await _policy(MemoryItemRepository()).evaluate(
        job=_job(),
        candidate=_candidate(structured_value={"temporal_scope": "current_turn"}),
        turns=[_turn()],
    )

    assert result.operation.operation == "noop"
    assert result.operation.reason_code == "current_turn_override"


def test_candidate_schema_rejects_blank_non_json_and_oversized_source_ref() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        _candidate(content="   ")
    with pytest.raises(ValueError, match="strict JSON"):
        _candidate(structured_value={"when": datetime.now(UTC)})
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="strict JSON"):
            _candidate(structured_value={"value": value})
    with pytest.raises(ValueError):
        _candidate(candidate_id="")
    with pytest.raises(ValueError):
        _job(source_refs=["x" * 129])
