import hashlib
import re

from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryCandidateSemantics,
    MemoryChangeIntent,
    MemoryFormationCandidate,
    MemoryFormationTrigger,
    MemoryPolarity,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemoryTemporalScope,
)

_COMPATIBLE_TRIGGERS = {
    MemoryFormationTrigger.STRUCTURED_EVENT,
    MemoryFormationTrigger.CONSOLIDATION,
    MemoryFormationTrigger.SWEEPER,
    MemoryFormationTrigger.MANUAL,
}


def adapt_structured_candidate(
    candidate: MemoryFormationCandidate,
    *,
    trigger: MemoryFormationTrigger,
) -> MemoryFormationCandidate:
    """Populate semantics only from existing structured fields for trusted legacy sources."""
    if candidate.semantic is not None or trigger not in _COMPATIBLE_TRIGGERS:
        return candidate
    scope = str(candidate.scope)
    raw_slot = str(
        candidate.structured_value.get("slot")
        or candidate.structured_value.get("object_type")
        or (
            "explicit_memory"
            if ":explicit:" in (candidate.memory_key_hint or "")
            else candidate.memory_key_hint
        )
        or "general"
    ).rsplit(":", 1)[-1]
    slot = (
        raw_slot
        if re.fullmatch(r"[a-z][a-z0-9_.~-]{0,127}", raw_slot)
        else f"field_{hashlib.sha256(raw_slot.encode()).hexdigest()[:32]}"
    )
    target = {
        "user_preference": MemorySemanticTarget.USER_PROFILE,
        "stable_fact": MemorySemanticTarget.USER_PROFILE,
        "task_memory": MemorySemanticTarget.TASK,
        "artifact_reference": MemorySemanticTarget.ARTIFACT,
        "session_summary": MemorySemanticTarget.SESSION,
    }.get(scope, MemorySemanticTarget.UNKNOWN)
    temporal_scope = {
        "task_memory": MemoryTemporalScope.CANONICAL,
        "artifact_reference": MemoryTemporalScope.CANONICAL,
        "session_summary": MemoryTemporalScope.SESSION,
    }.get(scope, MemoryTemporalScope.LONG_TERM)
    change_intent = {
        MemoryCandidateOperation.ADD: MemoryChangeIntent.SET,
        MemoryCandidateOperation.UPDATE: MemoryChangeIntent.REPLACE,
        MemoryCandidateOperation.DELETE: MemoryChangeIntent.DELETE,
        MemoryCandidateOperation.IGNORE: MemoryChangeIntent.NONE,
    }[candidate.proposed_operation]
    value = candidate.structured_value.get("value", candidate.content)
    if scope == "task_memory":
        value = candidate.structured_value.get("status")
    elif scope == "artifact_reference":
        value = {
            "artifact_id": candidate.structured_value.get("artifact_id"),
            "uri": candidate.structured_value.get("uri"),
        }
    return candidate.model_copy(
        update={
            "semantic": MemoryCandidateSemantics(
                target=target,
                slot=slot,
                value=value,
                temporal_scope=temporal_scope,
                polarity=MemoryPolarity.AFFIRMED,
                certainty=MemorySemanticCertainty.CERTAIN,
                change_intent=change_intent,
            )
        }
    )


__all__ = ["adapt_structured_candidate"]
