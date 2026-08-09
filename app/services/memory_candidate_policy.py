from collections.abc import Callable
from datetime import datetime

from app.core.config import Settings
from app.schemas.memory import (
    MemoryCandidateOperation,
    MemoryDecisionStatus,
    MemoryFormationCandidate,
    MemoryFormationJob,
    MemoryFormationReasonCode,
    MemoryFormationTurn,
    MemoryOperation,
    MemorySemanticVerification,
    MemorySemanticVerifierVerdict,
)
from app.services.memory_candidate_compatibility import adapt_structured_candidate
from app.services.memory_candidate_hard_rules import (
    CandidatePolicyResult,
    HardRuleEvaluation,
    MemoryCandidateHardRules,
    build_candidate_hash,
    build_memory_key,
)
from app.services.memory_candidate_safety_filter import TemporaryLanguageSafetyFilter
from app.services.memory_candidate_semantics import (
    MemoryCandidateSemanticValidator,
    SemanticValidationStatus,
)


class MemoryCandidatePolicy:
    def __init__(
        self,
        *,
        settings: Settings,
        repository,
        clock: Callable[[], datetime] | None = None,
        hard_rules: MemoryCandidateHardRules | None = None,
        semantic_validator: MemoryCandidateSemanticValidator | None = None,
        safety_filter: TemporaryLanguageSafetyFilter | None = None,
        verifier=None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.hard_rules = hard_rules or MemoryCandidateHardRules(
            repository=repository,
            clock=clock,
        )
        self.semantic_validator = semantic_validator or MemoryCandidateSemanticValidator()
        self.safety_filter = safety_filter or TemporaryLanguageSafetyFilter()
        self.verifier = verifier

    async def evaluate(
        self,
        *,
        job: MemoryFormationJob,
        candidate: MemoryFormationCandidate,
        turns: list[MemoryFormationTurn],
    ) -> CandidatePolicyResult:
        candidate = adapt_structured_candidate(candidate, trigger=job.trigger)
        hard = await self.hard_rules.evaluate(job=job, candidate=candidate, turns=turns)
        candidate = hard.candidate
        terminal = self._terminal(job, hard)
        if terminal is not None:
            return terminal

        semantic = self.semantic_validator.validate(candidate=candidate, current=hard.current)
        if semantic.status == SemanticValidationStatus.CURRENT_TURN:
            return self._result(
                job,
                hard,
                MemoryOperation.NOOP,
                MemoryDecisionStatus.NOOP,
                MemoryFormationReasonCode.CURRENT_TURN_OVERRIDE,
                semantic_outcome=semantic.status.value,
            )
        if candidate.confidence < self.settings.memory_formation_pending_threshold:
            return self._result(
                job,
                hard,
                MemoryOperation.REJECT,
                MemoryDecisionStatus.REJECTED,
                MemoryFormationReasonCode.CONFIDENCE_LOW,
                semantic_outcome=semantic.status.value,
            )
        safety = self.safety_filter.evaluate(candidate)
        if safety.downgrade:
            return self._result(
                job,
                hard,
                MemoryOperation.PENDING,
                MemoryDecisionStatus.PENDING,
                MemoryFormationReasonCode.AMBIGUOUS_CONFLICT,
                semantic_outcome=semantic.status.value,
                verifier_outcome=safety.filter_id,
            )

        verifier_outcome = None
        if semantic.status == SemanticValidationStatus.PENDING:
            hard, semantic, verifier_outcome = await self._verify_pending(
                job=job,
                hard=hard,
                turns=turns,
            )
            terminal = self._terminal(job, hard, verifier_outcome=verifier_outcome)
            if terminal is not None:
                return terminal
            if semantic.status != SemanticValidationStatus.CONFIRMED:
                return self._result(
                    job,
                    hard,
                    MemoryOperation.PENDING,
                    MemoryDecisionStatus.PENDING,
                    MemoryFormationReasonCode.AMBIGUOUS_DELETE
                    if candidate.proposed_operation == MemoryCandidateOperation.DELETE
                    else semantic.reason_code or MemoryFormationReasonCode.AMBIGUOUS_CONFLICT,
                    semantic_outcome=semantic.status.value,
                    verifier_outcome=verifier_outcome,
                )

        if candidate.confidence < self.settings.memory_formation_auto_threshold:
            return self._result(
                job,
                hard,
                MemoryOperation.PENDING,
                MemoryDecisionStatus.PENDING,
                MemoryFormationReasonCode.CONFIDENCE_PENDING,
                verifier_outcome=verifier_outcome,
            )
        if candidate.proposed_operation == MemoryCandidateOperation.DELETE:
            if not hard.delete_authorized:
                return self._result(
                    job,
                    hard,
                    MemoryOperation.PENDING,
                    MemoryDecisionStatus.PENDING,
                    MemoryFormationReasonCode.AMBIGUOUS_DELETE,
                    verifier_outcome=verifier_outcome,
                )
            return self._result(
                job,
                hard,
                MemoryOperation.DELETE,
                MemoryDecisionStatus.ACCEPTED,
                MemoryFormationReasonCode.AUTHORIZED_DELETE,
                verifier_outcome=verifier_outcome,
            )
        if hard.current is None:
            return self._result(
                job,
                hard,
                MemoryOperation.ADD,
                MemoryDecisionStatus.ACCEPTED,
                MemoryFormationReasonCode.ACCEPTED_NEW,
                verifier_outcome=verifier_outcome,
            )
        return self._result(
            job,
            hard,
            MemoryOperation.UPDATE,
            MemoryDecisionStatus.ACCEPTED,
            MemoryFormationReasonCode.ACCEPTED_UPDATE,
            verifier_outcome=verifier_outcome,
        )

    async def _verify_pending(
        self,
        *,
        job: MemoryFormationJob,
        hard: HardRuleEvaluation,
        turns: list[MemoryFormationTurn],
    ):
        if self.verifier is None:
            semantic = self.semantic_validator.validate(
                candidate=hard.candidate,
                current=hard.current,
            )
            return hard, semantic, "not_configured"
        try:
            raw = await self.verifier.verify(candidate=hard.candidate, turns=tuple(turns))
            verification = MemorySemanticVerification.model_validate(raw)
        except Exception:
            semantic = self.semantic_validator.validate(
                candidate=hard.candidate,
                current=hard.current,
            )
            return hard, semantic, "error"
        if verification.verdict != MemorySemanticVerifierVerdict.CONFIRMED:
            semantic = self.semantic_validator.validate(
                candidate=hard.candidate,
                current=hard.current,
            )
            return hard, semantic, verification.verdict.value

        rerun = await self.hard_rules.evaluate(
            job=job,
            candidate=hard.candidate,
            turns=turns,
        )
        semantic = self.semantic_validator.validate(
            candidate=_confirmed_copy(rerun.candidate),
            current=rerun.current,
        )
        return rerun, semantic, verification.verdict.value

    def _terminal(
        self,
        job: MemoryFormationJob,
        hard: HardRuleEvaluation,
        *,
        verifier_outcome: str | None = None,
    ) -> CandidatePolicyResult | None:
        if hard.terminal is None:
            return None
        return self._result(
            job,
            hard,
            hard.terminal.operation,
            hard.terminal.status,
            hard.terminal.reason,
            semantic_outcome="not_evaluated",
            verifier_outcome=verifier_outcome,
        )

    def _result(
        self,
        job: MemoryFormationJob,
        hard: HardRuleEvaluation,
        operation: MemoryOperation,
        status: MemoryDecisionStatus,
        reason: MemoryFormationReasonCode,
        *,
        semantic_outcome: str = "confirmed",
        verifier_outcome: str | None = None,
    ) -> CandidatePolicyResult:
        return self.hard_rules.result(
            job=job,
            evaluation=hard,
            operation=operation,
            status=status,
            reason=reason,
            semantic_outcome=semantic_outcome,
            verifier_outcome=verifier_outcome,
        )


def _confirmed_copy(candidate: MemoryFormationCandidate) -> MemoryFormationCandidate:
    semantic = candidate.semantic
    if semantic is None:
        return candidate
    return candidate.model_copy(
        update={
            "semantic": semantic.model_copy(update={"certainty": "certain"}),
        }
    )


__all__ = [
    "CandidatePolicyResult",
    "MemoryCandidatePolicy",
    "build_candidate_hash",
    "build_memory_key",
]
