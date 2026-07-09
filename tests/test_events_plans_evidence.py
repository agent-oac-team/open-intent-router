from pathlib import Path

from app.plugins.evidence import (
    EvidenceProviderScheduler,
    EvidenceResult,
    FileFixedQuestionEvidenceProvider,
    NoopEvidenceProvider,
)
from app.schemas.common import UserContext
from app.schemas.events import AgentEvent
from app.schemas.plans import Plan
from app.services.event_service import EventService
from app.services.plan_service import PlanService


async def test_agent_event_idempotency(repositories) -> None:
    service = EventService(repositories["events"])
    event = AgentEvent(
        event_id="e1",
        session_id="s1",
        agent_id="summarizer",
        event_type="agent_result",
    )
    first = await service.record_agent_event(event)
    second = await service.record_agent_event(event)
    assert not first.duplicate
    assert second.duplicate


async def test_plan_progresses_from_agent_event(repositories) -> None:
    service = PlanService(repositories["plans"])
    await service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "p1",
                "session_id": "s1",
                "steps": [
                    {"step_id": "s1", "agent_id": "summarizer", "description": "first"},
                    {
                        "step_id": "s2",
                        "agent_id": "summarizer",
                        "description": "second",
                        "depends_on": ["s1"],
                    },
                ],
            }
        )
    )
    updated = await service.apply_agent_event(
        AgentEvent(
            event_id="e1",
            session_id="s1",
            agent_id="summarizer",
            plan_id="p1",
            step_id="s1",
            event_type="agent_result",
        )
    )
    assert updated
    assert updated.current_step_id == "s2"


async def test_noop_evidence_provider() -> None:
    result = await NoopEvidenceProvider().match(
        question="anything",
        candidate_agent_ids=[],
        user=UserContext(id="u1"),
    )
    assert result.evidence == []


async def test_file_fixed_question_provider_strong_override(tmp_path: Path) -> None:
    path = tmp_path / "fixed.yaml"
    path.write_text(
        """
fixed_questions:
  - question: open dashboard
    strength: strong
    route_override:
      action: open_agent
      target_agent_id: handoff_dashboard
      message: ok
""",
        encoding="utf-8",
    )
    result = await FileFixedQuestionEvidenceProvider(str(path)).match(
        question="open dashboard",
        candidate_agent_ids=["handoff_dashboard"],
        user=UserContext(id="u1"),
    )
    assert result.route_override["target_agent_id"] == "handoff_dashboard"


async def test_file_fixed_question_provider_denies_unavailable_strong_override(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixed.yaml"
    path.write_text(
        """
fixed_questions:
  - question: open dashboard
    strength: strong
    route_override:
      action: open_agent
      target_agent_id: handoff_dashboard
      message: ok
""",
        encoding="utf-8",
    )
    result = await FileFixedQuestionEvidenceProvider(str(path)).match(
        question="open dashboard",
        candidate_agent_ids=["summarizer"],
        user=UserContext(id="u1"),
    )
    assert result.route_override is None
    assert result.route_override_denied["target_agent_id"] == "handoff_dashboard"
    assert result.route_override_denied["reason"] == "permission_denied"
    assert result.evidence[0]["route_override_denied"] is True


async def test_file_fixed_question_provider_weak_hint_does_not_override(tmp_path: Path) -> None:
    path = tmp_path / "fixed.yaml"
    path.write_text(
        """
fixed_questions:
  - question: help me choose
    strength: weak
    intent_hint: maybe_advisor
    candidate_agent_ids:
      - advisor
    route_override:
      action: open_agent
      target_agent_id: advisor
""",
        encoding="utf-8",
    )
    result = await FileFixedQuestionEvidenceProvider(str(path)).match(
        question="help me choose",
        candidate_agent_ids=["advisor"],
        user=UserContext(id="u1"),
    )
    assert result.intent_hint == "maybe_advisor"
    assert result.candidate_agent_ids == ["advisor"]
    assert result.route_override is None


async def test_evidence_scheduler_records_no_match_error_and_timeout() -> None:
    scheduler = EvidenceProviderScheduler(
        [
            ("no_match", StaticEvidenceProvider(EvidenceResult(metadata={"status": "no_match"}))),
            ("error", ErrorEvidenceProvider()),
            ("slow", SlowEvidenceProvider()),
        ],
        timeout_seconds=0.01,
    )

    result = await scheduler.match(
        question="anything",
        candidate_agent_ids=["summarizer"],
        user=UserContext(id="u1"),
    )

    scheduled = result.metadata["scheduled_providers"]
    assert scheduled[0]["result"]["status"] == "no_match"
    assert scheduled[1]["status"] == "error"
    assert scheduled[2]["status"] == "timeout"
    assert any("failed: error" in error for error in result.errors)
    assert any("timed out: slow" in error for error in result.errors)


class StaticEvidenceProvider:
    def __init__(self, result: EvidenceResult) -> None:
        self.result = result

    async def match(self, *, question, candidate_agent_ids, user):
        return self.result


class ErrorEvidenceProvider:
    async def match(self, *, question, candidate_agent_ids, user):
        raise RuntimeError("error")


class SlowEvidenceProvider:
    async def match(self, *, question, candidate_agent_ids, user):
        import asyncio

        await asyncio.sleep(0.05)
        return EvidenceResult()
