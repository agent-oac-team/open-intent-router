import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryChangeIntent,
    MemoryDecisionStatus,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTrigger,
    MemoryFormationTurn,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryOperation,
    MemoryPolarity,
    MemorySemanticCertainty,
    MemoryTemporalScope,
)

_ALLOWED_SCOPES = {
    "user_preference",
    "stable_fact",
    "task_memory",
    "artifact_reference",
    "session_summary",
}
_TRUSTED_CANONICAL_TRIGGERS = {
    MemoryFormationTrigger.STRUCTURED_EVENT,
    MemoryFormationTrigger.SWEEPER,
    MemoryFormationTrigger.CONSOLIDATION,
    MemoryFormationTrigger.MANUAL,
}
_SENSITIVE_PATTERNS = (
    re.compile(
        r"(?i)\b(?:password|passwd|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|session[_ -]?id|cookie|secret)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\bbasic\s+[a-z0-9+/=]{8,}"),
    re.compile(r"(?i)\b(?:otp|verification code|one[- ]time code)\s*[:=]?\s*\d{4,8}\b"),
    re.compile(r"(?:验证码|动态码)\s*[:=：]?\s*\d{4,8}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b\d{15,19}\b"),
    re.compile(r"\b\d{17}[0-9Xx]\b"),
    re.compile(r"(?i)\b(?:HIV|AIDS|medical record|diagnosed with)\b|(?:艾滋|病历|诊断为)"),
    re.compile(r"(?i)\b(?:home address|street address|address of)\b|(?:家庭住址|住址|地址是)"),
    re.compile(
        r"(?<!\d)(?:\+\d[\d ()-]{8,}\d|\(\d{2,4}\)[ -]?\d{3,4}[ -]?\d{4}|\d{3}[ -]\d{3}[ -]\d{4}|1[3-9]\d{9})(?!\d)"
    ),
)


@dataclass(frozen=True)
class HardRuleTerminal:
    operation: MemoryOperation
    status: MemoryDecisionStatus
    reason: MemoryFormationReasonCode


@dataclass(frozen=True)
class HardRuleEvaluation:
    candidate: MemoryFormationCandidate
    memory_key: str
    candidate_hash: str
    current: MemoryItem | None = None
    terminal: HardRuleTerminal | None = None
    delete_authorized: bool = False
    redacted: bool = False


@dataclass(frozen=True)
class CandidatePolicyResult:
    candidate: MemoryFormationCandidate
    operation: MemoryLifecycleOperation
    redacted_trace: dict


class MemoryCandidateHardRules:
    def __init__(
        self,
        *,
        repository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    async def evaluate(
        self,
        *,
        job: MemoryFormationJob,
        candidate: MemoryFormationCandidate,
        turns: list[MemoryFormationTurn],
    ) -> HardRuleEvaluation:
        memory_key = build_memory_key(
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            scope=str(candidate.scope),
            hint=candidate.semantic.slot
            if candidate.semantic is not None
            else candidate.memory_key_hint,
            structured=candidate.structured_value,
            allow_canonical_hint=job.trigger in _TRUSTED_CANONICAL_TRIGGERS,
        )
        candidate_hash = build_candidate_hash(candidate)

        reason = _validate_structured_candidate(job, candidate, memory_key)
        if reason is not None:
            return _terminal(candidate, memory_key, candidate_hash, reason)
        if _identity_mismatch(job, candidate) or str(candidate.scope) not in _ALLOWED_SCOPES:
            reason = (
                MemoryFormationReasonCode.IDENTITY_MISMATCH
                if _identity_mismatch(job, candidate)
                else MemoryFormationReasonCode.INVALID_SCOPE
            )
            return _terminal(candidate, memory_key, candidate_hash, reason)
        reason = _validate_evidence(job, candidate, turns)
        if reason is not None:
            return _terminal(candidate, memory_key, candidate_hash, reason)
        if await _is_recalled_memory_repetition(
            self.repository, job=job, candidate=candidate, turns=turns
        ):
            return _terminal(
                candidate,
                memory_key,
                candidate_hash,
                MemoryFormationReasonCode.RECALLED_MEMORY_REPETITION,
            )
        if _contains_sensitive_data(candidate) and (
            candidate.proposed_operation != MemoryCandidateOperation.DELETE
        ):
            return _terminal(
                candidate,
                memory_key,
                candidate_hash,
                MemoryFormationReasonCode.SENSITIVE_CONTENT,
                redacted=True,
            )

        current = None
        if _is_consolidation_target(candidate, job):
            targets = await self.repository.get_active_by_ids(
                [candidate.target_memory_id],
                tenant_id=job.tenant_id,
                subject_type="user",
                subject_id=job.user_id,
                user_id=job.user_id,
                scopes=[str(candidate.scope)],
            )
            if (
                len(targets) != 1
                or not targets[0].memory_key
                or candidate.memory_key_hint != targets[0].memory_key
            ):
                return _terminal(
                    candidate,
                    memory_key,
                    candidate_hash,
                    MemoryFormationReasonCode.IDENTITY_MISMATCH,
                )
            memory_key = targets[0].memory_key

        current = await self.repository.get_current_by_key(
            tenant_id=job.tenant_id,
            subject_type="user",
            subject_id=job.user_id,
            user_id=job.user_id,
            scope=str(candidate.scope),
            memory_key=memory_key,
            include_expired=True,
        )
        if candidate.target_memory_id and (
            current is None or current.memory_id != candidate.target_memory_id
        ):
            return _terminal(
                candidate,
                memory_key,
                candidate_hash,
                MemoryFormationReasonCode.IDENTITY_MISMATCH,
                current=current,
            )
        if (
            candidate.proposed_operation != MemoryCandidateOperation.DELETE
            and current
            and (current.candidate_hash == candidate_hash or same_value(current, candidate))
        ):
            return _terminal(
                candidate,
                memory_key,
                candidate_hash,
                MemoryFormationReasonCode.SAME_VALUE,
                operation=MemoryOperation.NOOP,
                status=MemoryDecisionStatus.NOOP,
                current=current,
            )
        if candidate.proposed_operation == MemoryCandidateOperation.IGNORE:
            return _terminal(
                candidate,
                memory_key,
                candidate_hash,
                MemoryFormationReasonCode.DUPLICATE_CANDIDATE,
                operation=MemoryOperation.NOOP,
                status=MemoryDecisionStatus.NOOP,
                current=current,
            )
        canonical_delete = self._canonical_delete(job, candidate, current)
        if canonical_delete is not None:
            operation, status, reason = canonical_delete
            return HardRuleEvaluation(
                candidate=candidate,
                memory_key=memory_key,
                candidate_hash=candidate_hash,
                current=current,
                terminal=HardRuleTerminal(operation, status, reason),
                redacted=_contains_sensitive_data(candidate),
            )
        return HardRuleEvaluation(
            candidate=candidate,
            memory_key=memory_key,
            candidate_hash=candidate_hash,
            current=current,
            delete_authorized=_authorized_user_delete(candidate, current),
            redacted=_contains_sensitive_data(candidate),
        )

    def _canonical_delete(
        self,
        job: MemoryFormationJob,
        candidate: MemoryFormationCandidate,
        current: MemoryItem | None,
    ) -> tuple[MemoryOperation, MemoryDecisionStatus, MemoryFormationReasonCode] | None:
        if candidate.proposed_operation != MemoryCandidateOperation.DELETE or current is None:
            return None
        if (
            job.trigger == MemoryFormationTrigger.CONSOLIDATION
            and _has_canonical_evidence(candidate)
            and candidate.structured_value.get("lifecycle_reason")
            in {"exact_duplicate", "session_compacted"}
        ):
            return (
                MemoryOperation.DELETE,
                MemoryDecisionStatus.ACCEPTED,
                MemoryFormationReasonCode.DUPLICATE_CANDIDATE,
            )
        if (
            job.trigger == MemoryFormationTrigger.SWEEPER
            and candidate.structured_value.get("lifecycle_reason") == "ttl_expired"
        ):
            if current.ttl_expires_at is None or _as_utc(current.ttl_expires_at) > _as_utc(
                self.clock()
            ):
                return (
                    MemoryOperation.REJECT,
                    MemoryDecisionStatus.REJECTED,
                    MemoryFormationReasonCode.INVALID_EVIDENCE,
                )
            return (
                MemoryOperation.DELETE,
                MemoryDecisionStatus.ACCEPTED,
                MemoryFormationReasonCode.TTL_EXPIRED,
            )
        return None

    def result(
        self,
        *,
        job: MemoryFormationJob,
        evaluation: HardRuleEvaluation,
        operation: MemoryOperation,
        status: MemoryDecisionStatus,
        reason: MemoryFormationReasonCode,
        semantic_outcome: str = "confirmed",
        verifier_outcome: str | None = None,
    ) -> CandidatePolicyResult:
        candidate = evaluation.candidate
        current = evaluation.current
        redacted = evaluation.redacted or _contains_sensitive_data(candidate)
        identity = "\x1f".join(
            (
                job.job_id,
                evaluation.memory_key,
                evaluation.candidate_hash,
                operation.value,
                status.value,
                reason.value,
            )
        )
        lifecycle = MemoryLifecycleOperation(
            operation_id=f"mfop_{hashlib.sha256(identity.encode()).hexdigest()[:32]}",
            operation=operation,
            decision_status=status,
            reason_code=reason,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            subject_type="user",
            subject_id=job.user_id,
            memory_key=evaluation.memory_key,
            candidate_hash=evaluation.candidate_hash,
            memory_id=current.memory_id if current else None,
            revision_id=current.current_revision_id if current else None,
            formation_job_id=job.job_id,
            canonical_refs=_bounded_source_refs(job.source_refs),
            metadata={
                "semantic_contract_version": "v1",
                "semantic_validation": semantic_outcome,
                **({"semantic_verifier": verifier_outcome} if verifier_outcome is not None else {}),
            },
        )
        trace = {
            "candidate_id": _redacted_ref(candidate.candidate_id)
            if redacted
            else candidate.candidate_id,
            "candidate_hash": evaluation.candidate_hash,
            "operation": operation.value,
            "decision_status": status.value,
            "reason_code": reason.value,
            "scope": str(candidate.scope),
            "memory_key": _redacted_ref(evaluation.memory_key)
            if redacted
            else evaluation.memory_key,
            "memory_id": current.memory_id if current else None,
            "source_refs": _bounded_source_refs(job.source_refs),
            "content_redacted": redacted,
            "semantic_contract_version": "v1",
            "semantic_validation": semantic_outcome,
        }
        if verifier_outcome is not None:
            trace["semantic_verifier"] = verifier_outcome
        return CandidatePolicyResult(candidate, lifecycle, trace)


def build_memory_key(
    *,
    tenant_id: str,
    user_id: str,
    scope: str,
    hint: str | None,
    structured: dict,
    allow_canonical_hint: bool = False,
) -> str:
    kind = {
        "user_preference": "preference",
        "stable_fact": "fact",
        "task_memory": "task",
        "artifact_reference": "artifact",
        "session_summary": "session_summary",
    }.get(scope, "invalid")
    raw_slot = str(structured.get("slot") or hint or structured.get("object_type") or "general")
    owner_prefix = (
        f"tenant:{_key_segment('tenant', tenant_id)}:user:{_key_segment('user', user_id)}:"
    )
    canonical_key = _canonical_memory_key(
        owner_prefix=owner_prefix, scope=scope, structured=structured
    )
    if allow_canonical_hint and canonical_key is not None:
        return canonical_key
    slot = raw_slot.rsplit(":", 1)[-1].strip().lower()
    if not re.fullmatch(r"[a-z0-9_.~-]{1,128}", slot):
        slot = f"sha256-{hashlib.sha256(raw_slot.encode()).hexdigest()}"
    value = f"{owner_prefix}{kind}:{slot}"
    if len(value) > 512:
        owner_hash = hashlib.sha256(f"{tenant_id}\x1f{user_id}".encode()).hexdigest()
        value = f"owner-sha256:{owner_hash}:{kind}:{slot}"
    return value


def build_candidate_hash(candidate: MemoryFormationCandidate) -> str:
    payload = {
        "operation": candidate.proposed_operation.value,
        "scope": str(candidate.scope),
        "content": _normalized_content(candidate.content),
        "structured_value": candidate.structured_value,
        "semantic": candidate.semantic.model_dump(mode="json") if candidate.semantic else None,
        "evidence_refs": sorted(
            (ref.model_dump(mode="json") for ref in candidate.evidence_refs),
            key=lambda value: json.dumps(value, sort_keys=True),
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def same_value(current: MemoryItem, candidate: MemoryFormationCandidate) -> bool:
    return _normalized_content(current.content) == _normalized_content(candidate.content) and (
        _semantic_structured_value(current.structured_value)
        == _semantic_structured_value(candidate.structured_value)
    )


def _terminal(
    candidate: MemoryFormationCandidate,
    memory_key: str,
    candidate_hash: str,
    reason: MemoryFormationReasonCode,
    *,
    operation: MemoryOperation = MemoryOperation.REJECT,
    status: MemoryDecisionStatus = MemoryDecisionStatus.REJECTED,
    current: MemoryItem | None = None,
    redacted: bool = False,
) -> HardRuleEvaluation:
    return HardRuleEvaluation(
        candidate=candidate,
        memory_key=memory_key,
        candidate_hash=candidate_hash,
        current=current,
        terminal=HardRuleTerminal(operation, status, reason),
        redacted=redacted or _contains_sensitive_data(candidate),
    )


def _identity_mismatch(job: MemoryFormationJob, candidate: MemoryFormationCandidate) -> bool:
    return bool(
        (candidate.tenant_id_hint and candidate.tenant_id_hint != job.tenant_id)
        or (candidate.subject_id_hint and candidate.subject_id_hint != job.user_id)
        or candidate.subject_type != "user"
        or candidate.structured_value.get("tenant_id") not in {None, job.tenant_id}
        or candidate.structured_value.get("user_id") not in {None, job.user_id}
    )


def _validate_evidence(
    job: MemoryFormationJob,
    candidate: MemoryFormationCandidate,
    turns: list[MemoryFormationTurn],
) -> MemoryFormationReasonCode | None:
    turn_by_id = {turn.turn_id: turn for turn in turns}
    if any(
        turn.tenant_id != job.tenant_id
        or turn.user_id != job.user_id
        or (job.session_id is not None and turn.session_id != job.session_id)
        for turn in turns
    ):
        return MemoryFormationReasonCode.IDENTITY_MISMATCH
    has_user = False
    has_canonical = False
    for evidence in candidate.evidence_refs:
        if evidence.role == "canonical":
            if job.trigger not in _TRUSTED_CANONICAL_TRIGGERS or (
                evidence.event_id not in job.source_refs
            ):
                return MemoryFormationReasonCode.INVALID_EVIDENCE
            has_canonical = True
            continue
        if evidence.turn_id not in turn_by_id or evidence.turn_id not in job.source_refs:
            return MemoryFormationReasonCode.INVALID_EVIDENCE
        turn = turn_by_id[evidence.turn_id]
        if evidence.role == "user":
            has_user = True
            if not evidence.quote or evidence.quote not in turn.user_text:
                return MemoryFormationReasonCode.INVALID_EVIDENCE
        elif evidence.quote and evidence.quote not in turn.assistant_text:
            return MemoryFormationReasonCode.INVALID_EVIDENCE
    if not has_user and not has_canonical and any(turn.used_memory_ids for turn in turns):
        return MemoryFormationReasonCode.RECALLED_MEMORY_REPETITION
    if not has_user and not has_canonical:
        return MemoryFormationReasonCode.ASSISTANT_ONLY_EVIDENCE
    if (
        job.trigger == MemoryFormationTrigger.CONSOLIDATION
        and str(candidate.scope) in {"user_preference", "stable_fact"}
        and not has_user
    ):
        kind = candidate.structured_value.get("consolidation_kind")
        if not (
            candidate.target_memory_id
            and (
                (kind == "exact_duplicate" and candidate.proposed_operation == "delete")
                or (kind == "semantic_duplicate" and candidate.proposed_operation == "update")
            )
        ):
            return MemoryFormationReasonCode.INVALID_EVIDENCE
    return None


async def _is_recalled_memory_repetition(
    repository,
    *,
    job: MemoryFormationJob,
    candidate: MemoryFormationCandidate,
    turns: list[MemoryFormationTurn],
) -> bool:
    used_ids = list(dict.fromkeys(item for turn in turns for item in turn.used_memory_ids))
    if not used_ids:
        return False
    if any(ref.role == "user" for ref in candidate.evidence_refs):
        return False
    get_active = getattr(repository, "get_active_by_ids", None)
    if not callable(get_active):
        return True
    recalled = await get_active(
        used_ids,
        tenant_id=job.tenant_id,
        subject_type="user",
        subject_id=job.user_id,
        user_id=job.user_id,
        scopes=None,
    )
    return any(
        _normalized_content(item.content) == _normalized_content(candidate.content)
        for item in recalled
    )


def _authorized_user_delete(
    candidate: MemoryFormationCandidate,
    current: MemoryItem | None,
) -> bool:
    semantic = candidate.semantic
    return bool(
        current is not None
        and candidate.target_memory_id == current.memory_id
        and any(ref.role == "user" and ref.quote for ref in candidate.evidence_refs)
        and semantic is not None
        and semantic.change_intent == MemoryChangeIntent.DELETE
        and semantic.temporal_scope == MemoryTemporalScope.LONG_TERM
        and semantic.polarity == MemoryPolarity.AFFIRMED
        and semantic.certainty == MemorySemanticCertainty.CERTAIN
    )


def _contains_sensitive_data(candidate: MemoryFormationCandidate) -> bool:
    value = "\n".join(
        (
            candidate.content,
            candidate.candidate_id,
            candidate.reason,
            candidate.memory_key_hint or "",
            candidate.target_memory_id or "",
            json.dumps(candidate.structured_value, ensure_ascii=False, default=str),
            json.dumps(
                candidate.semantic.model_dump(mode="json") if candidate.semantic else {},
                ensure_ascii=False,
                default=str,
            ),
            *(ref.quote or "" for ref in candidate.evidence_refs),
        )
    )
    return (
        candidate.sensitivity.lower() not in {"normal", "low", "none"}
        or candidate.structured_value.get("privacy_subject") == "third_party"
        or any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)
    )


def _validate_structured_candidate(
    job: MemoryFormationJob,
    candidate: MemoryFormationCandidate,
    rebuilt_key: str,
) -> MemoryFormationReasonCode | None:
    canonical_contract = _canonical_memory_key(
        owner_prefix="", scope=str(candidate.scope), structured=candidate.structured_value
    )
    if job.trigger != MemoryFormationTrigger.STRUCTURED_EVENT and canonical_contract is None:
        return None
    if str(candidate.scope) not in {"task_memory", "artifact_reference"}:
        return MemoryFormationReasonCode.INVALID_SCOPE
    if canonical_contract is None:
        return MemoryFormationReasonCode.INVALID_EVIDENCE
    if candidate.memory_key_hint != rebuilt_key:
        return MemoryFormationReasonCode.IDENTITY_MISMATCH
    return None


def _canonical_memory_key(*, owner_prefix: str, scope: str, structured: dict) -> str | None:
    object_type = structured.get("object_type")
    contracts = {
        ("task_memory", "plan"): ("plan_id", "task_status"),
        ("task_memory", "run"): ("run_id", "execution_status"),
        ("task_memory", "result"): ("result_id", "result_status"),
        ("artifact_reference", "artifact"): ("artifact_id", "reference"),
    }
    contract = contracts.get((scope, object_type))
    if contract is None:
        return None
    id_field, slot = contract
    object_id = structured.get(id_field)
    if not isinstance(object_id, str) or not object_id.strip() or len(object_id) > 128:
        return None
    return f"{owner_prefix}{object_type}:{_key_segment(object_type, object_id)}:{slot}"


def _is_consolidation_target(
    candidate: MemoryFormationCandidate,
    job: MemoryFormationJob,
) -> bool:
    return bool(
        job.trigger == MemoryFormationTrigger.CONSOLIDATION
        and candidate.target_memory_id
        and candidate.structured_value.get("consolidation_kind")
        in {"exact_duplicate", "semantic_duplicate", "session_summary"}
    )


def _has_canonical_evidence(candidate: MemoryFormationCandidate) -> bool:
    return any(ref.role == "canonical" for ref in candidate.evidence_refs)


def _semantic_structured_value(value: dict) -> dict:
    controls = {
        "change_intent",
        "delete_intent",
        "lifecycle_reason",
        "privacy_subject",
        "temporal_scope",
        "consolidation_kind",
        "duplicate_of",
        "potential_duplicate_id",
        "compacted_into",
        "source_memory_ids",
    }
    return {key: item for key, item in value.items() if key not in controls}


def _bounded_source_refs(values: list[str]) -> list[str]:
    values = [_safe_source_ref(value) for value in values]
    if len(values) <= 20:
        return list(values)
    omitted = values[20:]
    digest = hashlib.sha256(
        json.dumps(omitted, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return [*values[:20], f"omitted:{len(omitted)}:sha256:{digest}"]


def _key_segment(kind: str, value: str) -> str:
    encoded = quote(value, safe="-_.~")
    if len(encoded) <= 128:
        return encoded
    return f"{kind}-sha256-{hashlib.sha256(value.encode()).hexdigest()}"


def _normalized_content(value: str) -> str:
    return " ".join(value.split()).casefold()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _redacted_ref(value: str) -> str:
    return f"redacted:sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _safe_source_ref(value: str) -> str:
    if any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS):
        return _redacted_ref(value)
    return value if len(value) <= 128 else f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


__all__ = [
    "CandidatePolicyResult",
    "HardRuleEvaluation",
    "MemoryCandidateHardRules",
    "build_candidate_hash",
    "build_memory_key",
    "same_value",
]
