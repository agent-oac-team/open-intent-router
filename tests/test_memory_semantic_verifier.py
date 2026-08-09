import inspect

from app.llm.memory_semantic_verifier import FakeMemorySemanticVerifier
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryChangeIntent,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationTurn,
    MemoryPolarity,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemorySemanticVerification,
    MemorySemanticVerifierVerdict,
    MemoryTemporalScope,
)


async def test_verifier_contract_is_bounded_copying_and_side_effect_free() -> None:
    evidence = MemoryEvidenceRef(turn_id="turn_1", role="user", quote="以后请用中文回答")
    candidate = MemoryFormationCandidate(
        proposed_operation="add",
        scope="user_preference",
        content="用户偏好中文回答",
        structured_value={"slot": "response_language", "value": "zh"},
        semantic=MemoryCandidateSemantics(
            target=MemorySemanticTarget.ASSISTANT_RESPONSE,
            slot="response_language",
            value="zh",
            temporal_scope=MemoryTemporalScope.LONG_TERM,
            polarity=MemoryPolarity.AFFIRMED,
            certainty=MemorySemanticCertainty.UNCERTAIN,
            change_intent=MemoryChangeIntent.SET,
        ),
        confidence=0.8,
        evidence_refs=[evidence],
    )
    turn = MemoryFormationTurn(
        turn_id="turn_1",
        request_id="request_1",
        session_id="session_1",
        user_id="user_1",
        tenant_id="tenant_1",
        user_text="以后请用中文回答",
        result_status="completed",
    )
    verifier = FakeMemorySemanticVerifier(
        MemorySemanticVerification(
            verdict=MemorySemanticVerifierVerdict.UNCERTAIN,
            confidence=0.5,
            reason_code="insufficient_semantic_evidence",
            evidence_refs=[evidence],
        )
    )

    result = await verifier.verify(candidate=candidate, turns=[turn])
    result.evidence_refs.clear()

    assert verifier.response.verdict == MemorySemanticVerifierVerdict.UNCERTAIN
    assert verifier.response.evidence_refs == [evidence]
    assert set(inspect.signature(verifier.verify).parameters) == {"candidate", "turns"}
