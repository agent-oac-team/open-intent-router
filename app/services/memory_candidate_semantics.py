from dataclasses import dataclass
from enum import StrEnum

from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryChangeIntent,
    MemoryFormationCandidate,
    MemoryFormationReasonCode,
    MemoryItem,
    MemoryPolarity,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemoryTemporalScope,
)


class SemanticValidationStatus(StrEnum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    CURRENT_TURN = "current_turn"


@dataclass(frozen=True)
class SemanticValidationResult:
    status: SemanticValidationStatus
    reason_code: MemoryFormationReasonCode | None = None


class MemoryCandidateSemanticValidator:
    def validate(
        self,
        *,
        candidate: MemoryFormationCandidate,
        current: MemoryItem | None,
    ) -> SemanticValidationResult:
        semantic = candidate.semantic
        if semantic is None:
            return _pending()
        if semantic.temporal_scope == MemoryTemporalScope.CURRENT_TURN:
            return SemanticValidationResult(
                SemanticValidationStatus.CURRENT_TURN,
                MemoryFormationReasonCode.CURRENT_TURN_OVERRIDE,
            )
        if (
            semantic.target == MemorySemanticTarget.UNKNOWN
            or semantic.temporal_scope == MemoryTemporalScope.UNKNOWN
            or semantic.polarity == MemoryPolarity.UNKNOWN
            or semantic.certainty == MemorySemanticCertainty.UNCERTAIN
            or semantic.change_intent == MemoryChangeIntent.UNKNOWN
        ):
            return _pending()

        scope = str(candidate.scope)
        allowed_targets = {
            "user_preference": {
                MemorySemanticTarget.ASSISTANT_RESPONSE,
                MemorySemanticTarget.USER_PROFILE,
            },
            "stable_fact": {MemorySemanticTarget.USER_PROFILE},
            "task_memory": {MemorySemanticTarget.TASK},
            "artifact_reference": {MemorySemanticTarget.ARTIFACT},
            "session_summary": {MemorySemanticTarget.SESSION},
        }
        target_allowed = semantic.target in allowed_targets.get(scope, set()) or (
            candidate.proposed_operation == MemoryCandidateOperation.DELETE
            and semantic.target == MemorySemanticTarget.MEMORY
        )
        if not target_allowed:
            return _pending()
        allowed_temporal = {
            "user_preference": {MemoryTemporalScope.LONG_TERM},
            "stable_fact": {MemoryTemporalScope.LONG_TERM},
            "task_memory": {MemoryTemporalScope.CANONICAL},
            "artifact_reference": {MemoryTemporalScope.CANONICAL},
            "session_summary": {
                MemoryTemporalScope.SESSION,
                MemoryTemporalScope.CANONICAL,
            },
        }
        if semantic.temporal_scope not in allowed_temporal.get(scope, set()):
            return _pending()
        if semantic.polarity != MemoryPolarity.AFFIRMED:
            return _pending()

        expected_intent = {
            MemoryCandidateOperation.ADD: MemoryChangeIntent.SET,
            MemoryCandidateOperation.UPDATE: MemoryChangeIntent.REPLACE,
            MemoryCandidateOperation.DELETE: MemoryChangeIntent.DELETE,
            MemoryCandidateOperation.IGNORE: MemoryChangeIntent.NONE,
        }[candidate.proposed_operation]
        if semantic.change_intent != expected_intent:
            return _pending()

        structured_slot = candidate.structured_value.get("slot")
        if isinstance(structured_slot, str) and structured_slot != semantic.slot:
            return _pending()
        if scope in {"user_preference", "stable_fact"}:
            if (
                "value" in candidate.structured_value
                and candidate.structured_value["value"] != semantic.value
            ):
                return _pending()
        elif scope == "task_memory":
            if candidate.structured_value.get("status") != semantic.value:
                return _pending()
        elif scope == "artifact_reference":
            expected = {
                "artifact_id": candidate.structured_value.get("artifact_id"),
                "uri": candidate.structured_value.get("uri"),
            }
            if semantic.value != expected:
                return _pending()
        elif scope == "session_summary":
            expected = candidate.structured_value.get("value", candidate.content)
            if semantic.value != expected:
                return _pending()

        canonical_upsert = semantic.temporal_scope == MemoryTemporalScope.CANONICAL
        if (
            candidate.proposed_operation == MemoryCandidateOperation.UPDATE
            and current is None
            and not canonical_upsert
        ):
            return _pending()
        if (
            candidate.proposed_operation == MemoryCandidateOperation.ADD
            and current is not None
            and not canonical_upsert
        ):
            return _pending()
        return SemanticValidationResult(SemanticValidationStatus.CONFIRMED)


def _pending() -> SemanticValidationResult:
    return SemanticValidationResult(
        SemanticValidationStatus.PENDING,
        MemoryFormationReasonCode.AMBIGUOUS_CONFLICT,
    )


__all__ = [
    "MemoryCandidateSemanticValidator",
    "SemanticValidationResult",
    "SemanticValidationStatus",
]
