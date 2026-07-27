from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.events import agent_event
from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.plugins.evidence import (
    EvidenceProviderScheduler,
    EvidenceResult,
    FileFixedQuestionEvidenceProvider,
    NoopEvidenceProvider,
)
from app.repositories.database import (
    DatabaseEventRepository,
    DatabasePlanRepository,
    DatabaseRunRepository,
)
from app.repositories.memory import (
    MemoryEventRepository,
    MemoryPlanRepository,
    MemoryRunRepository,
)
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.schemas.common import UserContext
from app.schemas.events import AgentEvent
from app.schemas.logs import AgentRun
from app.schemas.plans import Plan
from app.services.event_service import EventService
from app.services.memory_integration import StructuredFormationPublisher
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
                "user_id": "u1",
                "tenant_id": "t1",
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
        ),
        tenant_id="t1",
        user_id="u1",
    )
    assert updated
    assert updated.current_step_id == "s2"


class FailOncePlanPublisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish_plan(self, plan, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("publisher unavailable")


async def test_duplicate_event_replays_plan_projection_after_first_publish_failure() -> None:
    events = MemoryEventRepository()
    plans = MemoryPlanRepository()
    runs = MemoryRunRepository()
    base_service = PlanService(plans)
    await base_service.save_plan(
        Plan(
            plan_id="p_retry",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            next_action={
                "type": "wait_for_agent_event",
                "plan_id": "p_state",
                "step_id": "s1",
            },
            steps=[{"step_id": "s1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    await runs.add_run(
        AgentRun(
            run_id="run_retry",
            session_id="s1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            plan_id="p_retry",
            step_id="s1",
            status="running",
            invoker_type="mock",
        )
    )
    publisher = FailOncePlanPublisher()
    service = PlanService(plans, structured_formation=publisher)
    payload = AgentEvent(
        event_id="event_retry",
        run_id="run_retry",
        session_id="s1",
        agent_id="summarizer",
        plan_id="p_retry",
        step_id="s1",
        event_type="agent_result",
    )

    first = await agent_event(
        payload,
        event_service=EventService(events),
        plan_service=service,
        run_repository=runs,
    )
    assert first.accepted
    stored = await service.get_plan("p_retry", tenant_id="t1", user_id="u1")
    assert stored and stored.status == "completed" and stored.last_event_id == "event_retry"

    retry = await agent_event(
        payload,
        event_service=EventService(events),
        plan_service=service,
        run_repository=runs,
    )
    assert retry.duplicate
    assert publisher.calls == 2


async def test_invalid_agent_event_status_is_rejected_before_durable_record() -> None:
    events = MemoryEventRepository()
    plans = MemoryPlanRepository()
    runs = MemoryRunRepository()
    service = PlanService(plans)
    await service.save_plan(
        Plan(
            plan_id="p_invalid_status",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "s1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    await runs.add_run(
        AgentRun(
            run_id="run_invalid_status",
            session_id="s1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            plan_id="p_invalid_status",
            step_id="s1",
            status="running",
            invoker_type="mock",
        )
    )
    invalid = AgentEvent(
        event_id="event_invalid_status",
        run_id="run_invalid_status",
        session_id="s1",
        agent_id="summarizer",
        plan_id="p_invalid_status",
        step_id="s1",
        event_type="agent_started",
        status="completed",
    )

    with pytest.raises(HTTPException) as error:
        await agent_event(
            invalid,
            event_service=EventService(events),
            plan_service=service,
            run_repository=runs,
        )
    assert error.value.status_code == 422
    assert await events.get_event("event_invalid_status", tenant_id="t1", user_id="u1") is None

    valid = await agent_event(
        invalid.model_copy(update={"status": "running"}),
        event_service=EventService(events),
        plan_service=service,
        run_repository=runs,
    )
    assert valid.accepted and not valid.duplicate


async def test_agent_event_mapping_and_terminal_state_do_not_regress() -> None:
    service = PlanService(MemoryPlanRepository())
    await service.save_plan(
        Plan(
            plan_id="p_state",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "s1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    started = await service.apply_agent_event(
        AgentEvent(
            event_id="event_started",
            session_id="s1",
            agent_id="summarizer",
            plan_id="p_state",
            step_id="s1",
            event_type="agent_started",
        ),
        tenant_id="t1",
        user_id="u1",
    )
    assert started and started.status == "running" and started.steps[0].status == "running"
    completed = await service.apply_agent_event(
        AgentEvent(
            event_id="event_completed",
            session_id="s1",
            agent_id="summarizer",
            plan_id="p_state",
            step_id="s1",
            event_type="agent_result",
        ),
        tenant_id="t1",
        user_id="u1",
    )
    assert completed and completed.status == "completed" and completed.current_step_id is None
    assert completed.next_action is None
    late = await service.apply_agent_event(
        AgentEvent(
            event_id="event_late_running",
            session_id="s1",
            agent_id="summarizer",
            plan_id="p_state",
            step_id="s1",
            event_type="agent_progress",
        ),
        tenant_id="t1",
        user_id="u1",
    )
    assert late and late.status == "completed" and late.steps[0].status == "completed"
    await service.save_plan(
        Plan(
            plan_id="p_conflict",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "s1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    with pytest.raises(ValueError, match="conflicts"):
        await service.apply_agent_event(
            AgentEvent(
                event_id="event_conflict",
                session_id="s1",
                agent_id="summarizer",
                plan_id="p_conflict",
                step_id="s1",
                event_type="agent_started",
                status="completed",
            ),
            tenant_id="t1",
            user_id="u1",
        )


async def test_late_progress_does_not_unblock_plan() -> None:
    service = PlanService(MemoryPlanRepository())
    await service.save_plan(
        Plan(
            plan_id="p_blocked",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "s1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    blocked = await service.apply_agent_event(
        AgentEvent(
            event_id="event_blocked",
            session_id="s1",
            agent_id="summarizer",
            plan_id="p_blocked",
            step_id="s1",
            event_type="agent_clarify",
        ),
        tenant_id="t1",
        user_id="u1",
    )
    late = await service.apply_agent_event(
        AgentEvent(
            event_id="event_late_progress",
            session_id="s1",
            agent_id="summarizer",
            plan_id="p_blocked",
            step_id="s1",
            event_type="agent_progress",
        ),
        tenant_id="t1",
        user_id="u1",
    )
    assert blocked is not None and blocked.status == "blocked"
    assert late is not None and late.status == "blocked"
    assert late.steps[0].status == "blocked"


async def test_plan_event_missing_owner_is_rejected_before_idempotency_record() -> None:
    events = MemoryEventRepository()
    payload = AgentEvent(
        event_id="owner-required",
        session_id="s1",
        agent_id="summarizer",
        plan_id="p1",
        step_id="s1",
        event_type="agent_result",
    )
    with pytest.raises(HTTPException) as error:
        await agent_event(
            payload,
            event_service=EventService(events),
            plan_service=PlanService(MemoryPlanRepository()),
            run_repository=MemoryRunRepository(),
        )
    assert error.value.status_code == 422
    assert await events.get_event(payload.event_id, tenant_id=None, user_id=None) is None


async def test_plan_event_uses_stored_run_owner_and_invalid_retry_does_not_poison() -> None:
    events = MemoryEventRepository()
    plans = MemoryPlanRepository()
    runs = MemoryRunRepository()
    plan_service = PlanService(plans)
    await plan_service.save_plan(
        Plan(
            plan_id="p1",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "step_1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    await runs.add_run(
        AgentRun(
            run_id="run_1",
            session_id="s1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            plan_id="p1",
            step_id="step_1",
            status="running",
            invoker_type="mock",
        )
    )
    invalid = AgentEvent(
        event_id="retryable-event",
        run_id="run_1",
        session_id="wrong-session",
        agent_id="summarizer",
        plan_id="p1",
        step_id="step_1",
        event_type="agent_result",
    )
    with pytest.raises(HTTPException) as error:
        await agent_event(
            invalid,
            event_service=EventService(events),
            plan_service=plan_service,
            run_repository=runs,
        )
    assert error.value.status_code == 404
    assert await events.get_event(invalid.event_id, tenant_id=None, user_id=None) is None

    response = await agent_event(
        invalid.model_copy(update={"session_id": "s1"}),
        event_service=EventService(events),
        plan_service=plan_service,
        run_repository=runs,
    )
    assert response.accepted
    stored = await plan_service.get_plan("p1", tenant_id="t1", user_id="u1")
    assert stored and stored.status == "completed"


async def test_private_run_agent_event_suppresses_plan_projection_atomically() -> None:
    settings = Settings(memory_mode="on")
    formation = MemoryFormationTurnJobRepository()
    publisher = StructuredFormationPublisher(settings=settings, repository=formation)
    events = MemoryEventRepository()
    plans = MemoryPlanRepository()
    runs = MemoryRunRepository()
    plan_service = PlanService(plans, structured_formation=publisher)
    await plan_service.save_plan(
        Plan(
            plan_id="private_event_plan",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "step_1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    initial_jobs = len(formation.jobs)
    await runs.add_run(
        AgentRun(
            run_id="private_event_run",
            session_id="s1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            plan_id="private_event_plan",
            step_id="step_1",
            status="running",
            invoker_type="mock",
            formation_suppressed=True,
        )
    )
    response = await agent_event(
        AgentEvent(
            event_id="private_agent_result",
            run_id="private_event_run",
            session_id="s1",
            agent_id="summarizer",
            plan_id="private_event_plan",
            step_id="step_1",
            event_type="agent_result",
        ),
        event_service=EventService(events),
        plan_service=plan_service,
        run_repository=runs,
    )
    assert response.accepted
    stored = await plan_service.get_plan("private_event_plan", tenant_id="t1", user_id="u1")
    assert stored is not None and stored.status == "completed"
    assert len(formation.jobs) == initial_jobs
    assert await plans.list_formation_pending() == []


async def test_plan_event_rejects_run_with_different_stored_owner() -> None:
    events = MemoryEventRepository()
    plans = MemoryPlanRepository()
    runs = MemoryRunRepository()
    plan_service = PlanService(plans)
    await plan_service.save_plan(
        Plan(
            plan_id="p1",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "step_1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    await runs.add_run(
        AgentRun(
            run_id="run_1",
            session_id="s1",
            agent_id="summarizer",
            user_id="u2",
            tenant_id="t1",
            plan_id="p1",
            step_id="step_1",
            status="running",
            invoker_type="mock",
        )
    )
    payload = AgentEvent(
        event_id="wrong-owner",
        run_id="run_1",
        session_id="s1",
        agent_id="summarizer",
        plan_id="p1",
        step_id="step_1",
        event_type="agent_result",
    )
    with pytest.raises(HTTPException) as error:
        await agent_event(
            payload,
            event_service=EventService(events),
            plan_service=plan_service,
            run_repository=runs,
        )
    assert error.value.status_code == 404
    assert await events.get_event(payload.event_id, tenant_id=None, user_id=None) is None


async def test_database_plan_event_round_trips_trusted_run_owner(tmp_path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'plan-event-owner.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    events = DatabaseEventRepository(session_factory)
    plans = DatabasePlanRepository(session_factory)
    runs = DatabaseRunRepository(session_factory)
    plan_service = PlanService(plans)
    await plan_service.save_plan(
        Plan(
            plan_id="p1",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            steps=[{"step_id": "step_1", "agent_id": "summarizer", "description": "run"}],
        )
    )
    await runs.add_run(
        AgentRun(
            run_id="run_1",
            session_id="s1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            plan_id="p1",
            step_id="step_1",
            status="running",
            invoker_type="mock",
            input={"_oir_execution": {"user_id": "forged"}},
        )
    )

    stored_run = await runs.get_run("run_1")
    assert stored_run
    assert stored_run.user_id == "u1"
    assert stored_run.tenant_id == "t1"
    assert stored_run.input == {"_oir_execution": {"user_id": "forged"}}
    response = await agent_event(
        AgentEvent(
            event_id="database-event",
            run_id="run_1",
            session_id="s1",
            agent_id="summarizer",
            plan_id="p1",
            step_id="step_1",
            event_type="agent_result",
        ),
        event_service=EventService(events),
        plan_service=plan_service,
        run_repository=runs,
    )
    assert response.accepted
    stored_plan = await plan_service.get_plan("p1", tenant_id="t1", user_id="u1")
    assert stored_plan and stored_plan.status == "completed"


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_run_repository_preserves_trusted_execution_identity(backend, tmp_path) -> None:
    if backend == "memory":
        runs = MemoryRunRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'run-identity.db'}",
        )
        await create_all_tables(settings)
        runs = DatabaseRunRepository(create_session_factory(settings))
    original = AgentRun(
        run_id="run_1",
        request_id="request_1",
        session_id="s1",
        agent_id="summarizer",
        user_id="u1",
        tenant_id="t1",
        plan_id="p1",
        step_id="step_1",
        status="running",
        invoker_type="mock",
        input={"nested": {"value": "original"}},
    )
    await runs.add_run(original)
    original.user_id = "attacker"
    original.input["nested"]["value"] = "attacker"
    loaded = await runs.get_run("run_1")
    assert loaded and loaded.user_id == "u1"
    assert loaded.input == {"nested": {"value": "original"}}
    loaded.tenant_id = "attacker-tenant"
    assert (await runs.get_run("run_1")).tenant_id == "t1"

    stored = await runs.get_run("run_1")
    with pytest.raises(ValueError, match="identity cannot be changed"):
        await runs.update_run(stored.model_copy(update={"user_id": "u2"}))
    updated = await runs.update_run(stored.model_copy(update={"status": "completed"}))
    assert updated.status == "completed"
    assert updated.user_id == "u1"


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
