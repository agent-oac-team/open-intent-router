import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.memory_runtime import build_memory_runtime_policy
from app.db.session import create_all_tables, create_session_factory
from app.invokers.local_function import LocalFunctionInvoker, LocalFunctionRegistry
from app.invokers.registry import AgentInvokerRegistry
from app.llm.conversation_formation import (
    ConversationFormationResponse,
    FakeConversationFormationModel,
    FormationModelInvalidResponse,
)
from app.repositories.context_stores import (
    DatabaseMemoryItemRepository,
    MemoryItemRepository,
)
from app.repositories.database import (
    DatabasePlanRepository,
    DatabaseResultRepository,
    DatabaseRunRepository,
)
from app.repositories.memory import (
    MemoryPlanRepository,
    MemoryResultRepository,
    MemoryRunRepository,
)
from app.repositories.memory_formation import (
    DatabaseMemoryFormationTurnJobRepository,
    MemoryFormationTurnJobRepository,
)
from app.repositories.memory_traces import MemoryFormationTraceRepository
from app.schemas.events import AgentEvent
from app.schemas.invocation import AgentInvocation, AgentInvocationResult, InvokeRequest
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryChangeIntent,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryFormationTurn,
    MemoryItem,
    MemoryPolarity,
    MemoryRecallRequest,
    MemorySemanticCertainty,
    MemorySemanticTarget,
    MemoryTemporalScope,
)
from app.schemas.plans import Plan
from app.schemas.routing import (
    InvocationPreview,
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.services.agent_context_service import AgentContextAssemblyService
from app.services.context_providers import ContextProviderContext, MemoryRetrievalProvider
from app.services.context_service import ContextService
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.memory_candidate_policy import MemoryCandidatePolicy
from app.services.memory_formation import (
    FormationJobWorker,
    FormationTriggerCoordinator,
    TurnCapsuleBuilder,
    TurnCaptureService,
    formation_turn_id,
)
from app.services.memory_integration import (
    FormationReconciler,
    MemoryFormationProcessor,
    StructuredFormationPublisher,
)
from app.services.memory_observability import MemoryObservabilityService
from app.services.memory_service import MemoryService
from app.services.plan_executor import PlanExecutor
from app.services.plan_service import PlanService
from app.services.router_service import RouterService
from app.services.task_continuation import TaskMemoryPlanResolver, requests_plan_continuation


def _settings(**updates) -> Settings:
    values = {
        "storage_backend": "memory",
        "memory_strategy_provider": "memory",
        "memory_mode": "on",
        "context_pipeline_mode": "enforced",
        "context_route_knowledge_enabled": False,
        "knowledge_enabled": False,
    }
    return Settings(**{**values, **updates})


def _plan(**updates) -> Plan:
    values = {
        "plan_id": "plan_continue",
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "old_session",
        "status": "pending",
        "steps": [
            {
                "step_id": "step_1",
                "agent_id": "summarizer",
                "description": "summarize the source",
            }
        ],
    }
    return Plan.model_validate({**values, **updates})


async def test_route_and_agent_memory_recall_enforce_canonical_agent_scope(
    registry_service,
) -> None:
    settings = _settings()
    repository = MemoryItemRepository()
    for memory_id, agent_id in (
        ("global", None),
        ("agent_a_private", "summarizer"),
        ("agent_b_private", "other-agent"),
    ):
        await repository.add(
            MemoryItem(
                memory_id=memory_id,
                scope="stable_fact",
                subject_type="user",
                subject_id="u1",
                user_id="u1",
                tenant_id="t1",
                agent_id=agent_id,
                content=memory_id,
            )
        )
    service = MemoryService(settings=settings, repository=repository)
    user = {"id": "u1", "attributes": {"tenant_id": "t1"}}
    route = await service.recall(
        MemoryRecallRequest(
            query="memory",
            user=user,
            scopes=["stable_fact"],
            subject_id="u1",
            agent_id=None,
            max_items=10,
        )
    )
    agent = await service.recall(
        MemoryRecallRequest(
            query="memory",
            user=user,
            scopes=["stable_fact"],
            subject_id="u1",
            agent_id="summarizer",
            max_items=10,
        )
    )
    assert [item.memory_id for item in route.context.items] == ["global"]
    assert {item.memory_id for item in agent.context.items} == {
        "global",
        "agent_a_private",
    }

    definition = await registry_service.get_definition("summarizer")
    assert definition is not None
    request = RouteRequest.model_validate(
        {
            "request_id": "scope-cache",
            "session_id": "s1",
            "user": user,
            "input": {"text": "memory"},
        }
    )
    route_provider = MemoryRetrievalProvider(settings, service, stage="route")
    agent_provider = MemoryRetrievalProvider(settings, service, stage="agent")
    route_context = ContextProviderContext(
        request=request,
        purpose="route",
        consumer="router",
    )
    agent_context = ContextProviderContext(
        request=request,
        purpose="agent_execution",
        consumer="agent",
        agent=definition,
    )
    assert route_provider.cache_key(route_context) != agent_provider.cache_key(agent_context)


async def test_structured_plan_jobs_are_idempotent_and_update_one_task_memory() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    memory_service = MemoryService(settings=settings, repository=memories)
    publisher = StructuredFormationPublisher(settings=settings, repository=formation)
    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=memories,
        model=FakeConversationFormationModel(),
        policy=MemoryCandidatePolicy(settings=settings, repository=memories),
        lifecycle=memory_service.lifecycle,
    )
    worker = FormationJobWorker(
        settings=settings,
        repository=formation,
        processor=processor,
        owner="integration-worker",
    )
    occurred = datetime(2026, 7, 13, tzinfo=UTC)
    plan = _plan()

    first = await publisher.publish_plan(
        plan,
        event_type="create",
        event_id="event_plan_create",
        source_version="plan-v1",
        occurred_at=occurred,
    )
    replay = await publisher.publish_plan(
        plan,
        event_type="create",
        event_id="event_plan_create",
        source_version="plan-v1",
        occurred_at=occurred,
    )

    assert first is not None and replay is not None
    assert first.job_id == replay.job_id
    assert len(formation.jobs) == 1
    completed = await worker.run_once()
    assert completed is not None and completed.status == "completed"
    assert completed.trace_summary["scopes"] == ["task_memory"]
    observability = MemoryObservabilityService(
        settings=settings,
        memory_service=memory_service,
        formation_repository=formation,
        trace_repository=MemoryFormationTraceRepository(
            formation_repository=formation,
            event_repository=memories,
        ),
    )
    metrics = await observability.metrics()
    assert metrics.jobs_by_trigger_scope_status == {"structured_event:task_memory:completed": 1}
    stored = await memories.list_active(
        tenant_id="t1", user_id="u1", scopes=["task_memory"], limit=10
    )
    assert len(stored) == 1
    memory_id = stored[0].memory_id
    assert stored[0].structured_value["plan_id"] == plan.plan_id
    assert stored[0].current_revision_no == 1

    running = plan.model_copy(
        update={
            "status": "running",
            "steps": [plan.steps[0].model_copy(update={"status": "running"})],
        }
    )
    await publisher.publish_plan(
        running,
        event_type="update",
        event_id="event_plan_running",
        source_version="plan-v2",
        occurred_at=occurred + timedelta(seconds=1),
    )
    completed = await worker.run_once()
    assert completed is not None and completed.status == "completed"
    stored = await memories.list_active(
        tenant_id="t1", user_id="u1", scopes=["task_memory"], limit=10
    )
    assert len(stored) == 1
    assert stored[0].memory_id == memory_id
    assert stored[0].current_revision_no == 2
    assert stored[0].structured_value["status"] == "running"


async def test_out_of_order_structured_workers_cannot_regress_task_memory() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    memory_service = MemoryService(settings=settings, repository=memories)
    publisher = StructuredFormationPublisher(settings=settings, repository=formation)
    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=memories,
        model=FakeConversationFormationModel(),
        policy=MemoryCandidatePolicy(settings=settings, repository=memories),
        lifecycle=memory_service.lifecycle,
    )
    initial = _plan()
    running = initial.model_copy(
        update={
            "status": "running",
            "steps": [initial.steps[0].model_copy(update={"status": "running"})],
        }
    )
    first_at = datetime(2026, 7, 13, tzinfo=UTC)
    await publisher.publish_plan(
        initial,
        event_type="create",
        event_id="event_old_pending",
        source_version="plan-old",
        occurred_at=first_at,
    )
    await publisher.publish_plan(
        running,
        event_type="update",
        event_id="event_new_running",
        source_version="plan-new",
        occurred_at=first_at + timedelta(seconds=1),
    )
    old_job = await formation.claim_job(
        owner="old-worker",
        now=first_at + timedelta(seconds=2),
        lease_seconds=60,
    )
    new_job = await formation.claim_job(
        owner="new-worker",
        now=first_at + timedelta(seconds=2),
        lease_seconds=60,
    )
    assert old_job is not None and new_job is not None

    new_trace = await processor.process(new_job, execute_lifecycle=True)
    await formation.complete_job(
        new_job.job_id,
        owner="new-worker",
        lease_token=new_job.lease_token,
        now=first_at + timedelta(seconds=3),
        trace_summary=new_trace,
    )
    old_trace = await processor.process(old_job, execute_lifecycle=True)
    await formation.complete_job(
        old_job.job_id,
        owner="old-worker",
        lease_token=old_job.lease_token,
        now=first_at + timedelta(seconds=3),
        trace_summary=old_trace,
    )

    stored = await memories.list_active(
        tenant_id="t1", user_id="u1", scopes=["task_memory"], limit=10
    )
    assert len(stored) == 1
    assert stored[0].structured_value["status"] == "running"
    assert stored[0].current_revision_no == 1
    assert old_trace["operation_counts"] == {"noop": 1}
    assert any(
        event.payload.get("operation", {}).get("reason_code") == "duplicate_candidate"
        for event in memories.events
        if isinstance(event.payload, dict)
    )


async def test_structured_job_survives_database_process_restart(tmp_path) -> None:
    settings = _settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'structured-restart.db'}",
    )
    await create_all_tables(settings)
    first_factory = create_session_factory(settings)
    first_repository = DatabaseMemoryFormationTurnJobRepository(first_factory)
    occurred = datetime(2026, 7, 13, tzinfo=UTC)
    published = await StructuredFormationPublisher(
        settings=settings,
        repository=first_repository,
    ).publish_plan(
        _plan(),
        event_type="create",
        event_id="event_database_plan",
        source_version="database-plan-v1",
        occurred_at=occurred,
    )
    assert published is not None
    await first_factory.kw["bind"].dispose()

    second_factory = create_session_factory(settings)
    restarted = DatabaseMemoryFormationTurnJobRepository(second_factory)
    try:
        claimed = await restarted.claim_job(
            owner="restarted-worker",
            now=occurred + timedelta(seconds=1),
            lease_seconds=60,
        )
        assert claimed is not None
        assert claimed.job_id == published.job_id
        assert claimed.trace_summary["command"]["source_id"] == "plan_continue"
        assert claimed.trace_summary["command"]["candidates"][0]["scope"] == "task_memory"
    finally:
        await second_factory.kw["bind"].dispose()


@pytest.mark.parametrize(
    ("mode", "expected_items", "expected_status"),
    [("observe", 0, "observed"), ("on", 1, "accepted")],
)
async def test_real_processor_runs_conversation_jobs_through_rollout_mode(
    mode,
    expected_items,
    expected_status,
) -> None:
    settings = _settings(memory_mode=mode, memory_formation_window_turns=1)
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    memory_service = MemoryService(settings=settings, repository=memories)
    turn = MemoryFormationTurn(
        turn_id="turn_processor",
        request_id="request_processor",
        session_id="session_processor",
        run_id="run_processor",
        user_id="u1",
        tenant_id="t1",
        agent_id="summarizer",
        user_text="以后请用中文回答",
        assistant_text="好的",
        result_status="completed",
        completed_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    coordinator = FormationTriggerCoordinator(settings=settings, repository=formation)
    _, job = await coordinator.append_and_check(turn)
    assert job is not None
    candidate = MemoryFormationCandidate(
        candidate_id="candidate_processor",
        proposed_operation="add",
        scope="user_preference",
        content="用户偏好以后使用中文回答",
        structured_value={"slot": "response_language", "value": "zh"},
        semantic=MemoryCandidateSemantics(
            target=MemorySemanticTarget.ASSISTANT_RESPONSE,
            slot="response_language",
            value="zh",
            temporal_scope=MemoryTemporalScope.LONG_TERM,
            polarity=MemoryPolarity.AFFIRMED,
            certainty=MemorySemanticCertainty.CERTAIN,
            change_intent=MemoryChangeIntent.SET,
        ),
        subject_id_hint="u1",
        tenant_id_hint="t1",
        memory_key_hint="response_language",
        confidence=0.99,
        evidence_refs=[
            MemoryEvidenceRef(
                turn_id=turn.turn_id,
                role="user",
                quote="以后请用中文回答",
            )
        ],
    )
    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=memories,
        model=FakeConversationFormationModel(
            ConversationFormationResponse(candidates=[candidate]),
            usage={"prompt_tokens": 11, "completion_tokens": 5},
        ),
        policy=MemoryCandidatePolicy(settings=settings, repository=memories),
        lifecycle=memory_service.lifecycle,
    )
    worker = FormationJobWorker(
        settings=settings,
        repository=formation,
        processor=processor,
        owner=f"worker-{mode}",
    )

    completed = await worker.run_once()

    assert completed is not None and completed.status == "completed"
    assert completed.trace_summary["decision_counts"] == {expected_status: 1}
    assert completed.trace_summary["semantic_contract_version"] == "v1"
    assert completed.trace_summary["semantic_validation_counts"] == {"confirmed": 1}
    assert completed.trace_summary["semantic_verifier_counts"] == {}
    assert "semantic_value" not in completed.trace_summary
    assert completed.trace_summary["model_latency_ms"] >= 0
    assert completed.trace_summary["usage"] == {"prompt_tokens": 11, "completion_tokens": 5}
    stored = await memories.list_active(tenant_id="t1", user_id="u1", limit=10)
    assert len(stored) == expected_items
    if mode == "observe":
        assert memories.events[-1].decision_status == "observed"


async def test_processor_revalidates_injected_model_semantics_before_policy() -> None:
    settings = _settings(memory_formation_window_turns=1)
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    turn = MemoryFormationTurn(
        turn_id="turn_untrusted_model",
        request_id="request_untrusted_model",
        session_id="session_untrusted_model",
        run_id="run_untrusted_model",
        user_id="u1",
        tenant_id="t1",
        user_text="以后请用中文回答",
        assistant_text="好的",
        result_status="completed",
    )
    _, job = await FormationTriggerCoordinator(
        settings=settings, repository=formation
    ).append_and_check(turn)
    assert job is not None
    candidate = MemoryFormationCandidate(
        proposed_operation="add",
        scope="user_preference",
        content="用户偏好中文回答",
        structured_value={"slot": "response_language", "value": "zh"},
        confidence=0.99,
        evidence_refs=[MemoryEvidenceRef(turn_id=turn.turn_id, role="user", quote=turn.user_text)],
    )

    class UnvalidatedModel:
        last_usage = {}

        async def form(self, **_kwargs):
            return [candidate]

    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=memories,
        model=UnvalidatedModel(),
        policy=MemoryCandidatePolicy(settings=settings, repository=memories),
        lifecycle=MemoryService(settings=settings, repository=memories).lifecycle,
    )

    with pytest.raises(FormationModelInvalidResponse):
        await processor.process(job, execute_lifecycle=True)

    assert (
        await memories.list_active(
            tenant_id="t1",
            user_id="u1",
            subject_type="user",
            subject_id="u1",
            limit=10,
        )
        == []
    )


class CapturingRouteLLM:
    def __init__(self) -> None:
        self.payloads: list[LLMRouteInput] = []

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        self.payloads.append(payload)
        return RouteResponse(
            request_id=payload.request.request_id or "request_route",
            session_id=payload.request.session_id,
            decision=RouteDecision(action="reply", message="ok"),
            context=payload.context,
        )


async def test_task_memory_resolves_owned_canonical_plan_only_for_continuation(
    registry_service,
) -> None:
    settings = _settings()
    memories = MemoryItemRepository()
    memory_service = MemoryService(settings=settings, repository=memories)
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    plan = _plan(status="running")
    await plan_service.save_plan(plan)
    await memories.add(
        MemoryItem(
            memory_id="memory_task_plan",
            scope="task_memory",
            subject_type="user",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="Unfinished plan pointer",
            structured_value={
                "object_type": "plan",
                "plan_id": plan.plan_id,
                "status": "pending",
                "tenant_id": "t1",
                "user_id": "u1",
            },
            current_revision_id="revision_task_plan",
            current_revision_no=1,
        )
    )
    await memories.add(
        MemoryItem(
            memory_id="memory_other_plan",
            scope="task_memory",
            subject_type="user",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="Cancelled other Plan",
            structured_value={
                "object_type": "plan",
                "plan_id": "other_plan",
                "status": "cancelled",
                "tenant_id": "t1",
                "user_id": "u1",
            },
        )
    )
    await memories.add(
        MemoryItem(
            memory_id="memory_old_run",
            scope="task_memory",
            subject_type="user",
            subject_id="u1",
            user_id="u1",
            tenant_id="t1",
            content="Old failed Run",
            structured_value={
                "object_type": "run",
                "run_id": "old_run",
                "status": "failed",
                "tenant_id": "t1",
                "user_id": "u1",
            },
        )
    )
    llm = CapturingRouteLLM()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=llm,
        context_service=ContextService(settings, memory_service=memory_service),
        plan_service=plan_service,
        plan_continuation_resolver=TaskMemoryPlanResolver(
            memory_service=memory_service,
            plan_service=plan_service,
        ),
    )

    continued = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "new_session",
                "user": {
                    "id": "u1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
                "input": {"text": "继续上次任务"},
            }
        )
    )
    assert "plan_id" in continued.context.metadata["active_plan_keys"]
    continued_items = llm.payloads[-1].projection.payload["context"]["items"]
    canonical_plan = next(
        item for item in continued_items if item["item_id"] == "current_plan:plan_continue"
    )
    assert canonical_plan["authority"] == "authoritative"
    assert canonical_plan["structured_value"]["status"] == "running"
    assert not any(item["source"] == "memory" for item in continued_items)

    unrelated = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "unrelated_session",
                "user": {
                    "id": "u1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
                "input": {"text": "总结这篇全新的文章"},
            }
        )
    )
    assert "active_plan_keys" not in unrelated.context.metadata
    assert not any(
        item["source"] == "memory"
        for item in llm.payloads[-1].projection.payload["context"]["items"]
    )

    await plan_service.cancel(plan.plan_id, tenant_id="t1", user_id="u1")
    terminal = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "terminal_plan_session",
                "user": {
                    "id": "u1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
                "input": {"text": "继续上次任务"},
            }
        )
    )
    assert "active_plan_keys" not in terminal.context.metadata
    assert not any(
        item["source"] == "memory"
        for item in llm.payloads[-1].projection.payload["context"]["items"]
    )

    other_user = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "other_user_session",
                "user": {
                    "id": "u2",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
                "input": {"text": "继续上次任务"},
            }
        )
    )
    assert "active_plan_keys" not in other_user.context.metadata
    assert not any(
        item["source"] == "memory"
        for item in llm.payloads[-1].projection.payload["context"]["items"]
    )


class InspectingCapture:
    def __init__(self, runs, results, *, fail: bool = False) -> None:
        self.runs = runs
        self.results = results
        self.fail = fail
        self.calls = []

    async def capture(self, *, invocation, result, result_id, completed_at=None):
        assert await self.runs.get_run(invocation.run_id) is not None
        assert any(
            item.result_id == result_id
            for item in await self.results.list_recent(
                "s1",
                tenant_id=invocation.user.tenant_id or "",
                user_id=invocation.user.id,
            )
        )
        self.calls.append((invocation, result, result_id))
        if self.fail:
            raise RuntimeError("capture unavailable")


class CapturingStructuredSink:
    def __init__(self) -> None:
        self.run_events = []
        self.results = []
        self.plan_events = []

    async def publish_run(self, run, *, event_type, **kwargs):
        self.run_events.append((event_type, run.status))

    async def publish_result(self, result, *, run, **kwargs):
        self.results.append((result.result_id, run.run_id))

    async def publish_plan(self, plan, *, event_type, **kwargs):
        self.plan_events.append((event_type, plan.status, kwargs))


class FailingPlanSink:
    async def publish_plan(self, plan, *, event_type, **kwargs):
        raise RuntimeError("formation store down")


class FailingStructuredSink(FailingPlanSink):
    async def publish_run(self, run, *, event_type, **kwargs):
        raise RuntimeError("formation store down")


class FailingMarkerRunRepository(MemoryRunRepository):
    async def mark_formation_published(self, run_id: str, *, source_order: int) -> None:
        raise RuntimeError("marker store down")


class FailingMarkerResultRepository(MemoryResultRepository):
    async def mark_formation_published(self, result_id: str) -> None:
        raise RuntimeError("marker store down")

    async def mark_turn_captured(self, result_id: str) -> None:
        raise RuntimeError("marker store down")


class FailOnceTurnMarkerMemoryResultRepository(MemoryResultRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def mark_turn_captured(self, result_id: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("turn marker store down")
        await super().mark_turn_captured(result_id)


class FailOnceTurnMarkerDatabaseResultRepository(DatabaseResultRepository):
    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self.failed = False

    async def mark_turn_captured(self, result_id: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("turn marker store down")
        await super().mark_turn_captured(result_id)


class FailingMarkerPlanRepository(MemoryPlanRepository):
    async def mark_formation_published(self, plan_id: str, *, state_version: int) -> None:
        raise RuntimeError("marker store down")


class FailOnceMarkerPlanRepository(MemoryPlanRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def mark_formation_published(self, plan_id: str, *, state_version: int) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("marker store down")
        await super().mark_formation_published(plan_id, state_version=state_version)


class FailFirstRunCreateSink(CapturingStructuredSink):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def publish_run(self, run, *, event_type, **kwargs):
        if event_type == "create" and not self.failed:
            self.failed = True
            raise RuntimeError("create publish down")
        await super().publish_run(run, event_type=event_type, **kwargs)

    async def publish_result(self, result, *, run, **kwargs):
        raise RuntimeError("formation store down")


async def test_plan_service_publishes_create_confirm_agent_update_and_cancel() -> None:
    sink = CapturingStructuredSink()
    service = PlanService(MemoryPlanRepository(), structured_formation=sink)
    plan = _plan()

    await service.save_plan(plan)
    await service.confirm(plan.plan_id, tenant_id="t1", user_id="u1")
    await service.apply_agent_event(
        AgentEvent(
            event_id="event_agent_running",
            run_id="run_plan",
            session_id="old_session",
            agent_id="summarizer",
            event_type="agent_progress",
            status="running",
            plan_id=plan.plan_id,
            step_id="step_1",
            created_at=datetime(2026, 7, 13, tzinfo=UTC),
        ),
        tenant_id="t1",
        user_id="u1",
    )
    await service.cancel(plan.plan_id, tenant_id="t1", user_id="u1")

    assert [event[0] for event in sink.plan_events] == [
        "create",
        "confirm",
        "update",
        "cancel",
    ]
    assert sink.plan_events[2][2]["event_id"] == "event_agent_running"
    assert sink.plan_events[2][2]["source_version"] == "event_agent_running"


async def test_plan_claim_publishes_running_transition_and_failure_does_not_escape() -> None:
    sink = CapturingStructuredSink()
    service = PlanService(MemoryPlanRepository(), structured_formation=sink)
    await service.save_plan(_plan())
    claimed = await service.claim_step(_plan().plan_id, "step_1", tenant_id="t1", user_id="u1")
    assert claimed is not None
    assert sink.plan_events[-1][1] == "running"
    assert sink.plan_events[-1][2]["source_order"] == claimed[0].state_version

    failing = PlanService(MemoryPlanRepository(), structured_formation=FailingPlanSink())
    stored = await failing.save_plan(_plan(plan_id="plan_publish_failure"))
    assert stored.plan_id == "plan_publish_failure"


async def test_marker_failures_do_not_escape_completed_plan_or_invocation(
    settings,
    registry_service,
) -> None:
    plan = await PlanService(
        FailingMarkerPlanRepository(),
        structured_formation=CapturingStructuredSink(),
    ).save_plan(_plan(plan_id="plan_marker_failure"))
    assert plan.plan_id == "plan_marker_failure"

    runs = FailingMarkerRunRepository()
    results = FailingMarkerResultRepository()
    service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        invokers=build_default_invoker_registry(settings),
        structured_formation=CapturingStructuredSink(),
        turn_capture=InspectingCapture(runs, results),
    )
    response = await service.invoke(
        InvokeRequest(
            request_id="marker_failure",
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "still return success"},
        )
    )
    assert response.status == "completed"
    assert len(await runs.list_formation_pending()) == 1
    assert len(await results.list_formation_pending()) == 1


async def test_terminal_run_retries_missing_create_transition_before_completion(
    settings,
    registry_service,
) -> None:
    sink = FailFirstRunCreateSink()
    runs = MemoryRunRepository()
    service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=MemoryResultRepository(),
        invokers=build_default_invoker_registry(settings),
        structured_formation=sink,
    )
    response = await service.invoke(
        InvokeRequest(
            request_id="run_transition_retry",
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "run"},
        )
    )
    assert response.status == "completed"
    assert sink.run_events == [("create", "running"), ("complete", "completed")]
    run_id = next(iter(runs.runs))
    assert await runs.get_formation_published_order(run_id) == 3


async def test_plan_reconciler_replays_exact_durable_command_after_marker_failure() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    repository = FailOnceMarkerPlanRepository()
    publisher = StructuredFormationPublisher(settings=settings, repository=formation)
    service = PlanService(repository, structured_formation=publisher)
    stored = await service.save_plan(_plan(plan_id="plan_exact_replay"))
    assert stored.formation_event_type == "create"
    assert len(formation.jobs) == 1
    assert len(await repository.list_formation_pending()) == 1
    reconciler = FormationReconciler(
        plan_repository=repository,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        publisher=publisher,
        turn_capture=None,
    )
    assert await reconciler.run_once() == {
        "plans": 1,
        "runs": 0,
        "results": 0,
        "turns": 0,
    }
    assert len(formation.jobs) == 1
    assert await repository.list_formation_pending() == []


async def test_reconciler_rejects_cross_owner_result_without_projection_or_capsule() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    run = await runs.add_run(
        AgentRun(
            run_id="run_owner",
            session_id="s1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            status="completed",
            invoker_type="mock",
        )
    )
    await runs.mark_formation_published(run.run_id, source_order=3)
    await results.add_result(
        AgentResult(
            result_id="result_wrong_owner",
            run_id=run.run_id,
            session_id=run.session_id,
            agent_id=run.agent_id,
            user_id="u2",
            tenant_id="t2",
            status="completed",
            message="u2 private output",
        )
    )
    publisher = StructuredFormationPublisher(settings=settings, repository=formation)
    with pytest.raises(ValueError, match="canonical Run"):
        await publisher.publish_result(results.results[0], run=run)
    reconciler = FormationReconciler(
        plan_repository=MemoryPlanRepository(),
        run_repository=runs,
        result_repository=results,
        publisher=publisher,
        turn_capture=TurnCaptureService(
            settings=settings,
            builder=TurnCapsuleBuilder(settings),
            coordinator=FormationTriggerCoordinator(settings=settings, repository=formation),
            event_repository=memories,
        ),
    )
    assert await reconciler.run_once() == {
        "plans": 0,
        "runs": 0,
        "results": 0,
        "turns": 0,
    }
    assert formation.jobs == {} and formation.turns == {}
    assert await results.list_formation_pending() == []


async def test_database_recovered_turn_capsule_matches_direct_fields(tmp_path) -> None:
    settings = _settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'turn-equivalence.db'}",
    )
    await create_all_tables(settings)
    session_factory = create_session_factory(settings)
    runs = DatabaseRunRepository(session_factory)
    results = DatabaseResultRepository(session_factory)
    run = AgentRun(
        run_id="run_turn_equal",
        request_id="request_turn_equal",
        session_id="s1",
        agent_id="summarizer",
        user_id="u1",
        tenant_id="t1",
        plan_id="plan_equal",
        step_id="step_equal",
        status="completed",
        invoker_type="mock",
        input={"text": "question", "_oir_execution": {"business": "keep-run"}},
        used_memory_ids=["memory_1"],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = AgentResult(
        result_id="result_turn_equal",
        run_id=run.run_id,
        session_id=run.session_id,
        agent_id=run.agent_id,
        user_id=run.user_id,
        tenant_id=run.tenant_id,
        plan_id=run.plan_id,
        step_id=run.step_id,
        status="completed",
        message="done",
        output={"summary": "visible", "_execution": {"business": "keep-result"}},
        created_at=datetime.now(UTC),
    )
    builder = TurnCapsuleBuilder(settings)
    direct = builder.build_from_records(run=run, result=result)
    await runs.add_run(run)
    await results.add_result(result)
    stored_run = await runs.get_run(run.run_id)
    stored_result = (await results.list_recent("s1", tenant_id="t1", user_id="u1"))[0]
    assert stored_run is not None
    assert stored_run.input == run.input
    assert stored_result.output == result.output
    recovered = builder.build_from_records(run=stored_run, result=stored_result)
    assert recovered.model_dump(exclude={"created_at", "completed_at"}) == direct.model_dump(
        exclude={"created_at", "completed_at"}
    )
    assert '"business": "keep-result"' in recovered.assistant_text
    await session_factory.kw["bind"].dispose()


async def test_invocation_captures_after_run_result_and_capture_failure_keeps_success(
    settings,
    registry_service,
) -> None:
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    capture = InspectingCapture(runs, results, fail=True)
    structured = CapturingStructuredSink()
    service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        invokers=build_default_invoker_registry(settings),
        turn_capture=capture,
        structured_formation=structured,
    )

    result = await service.invoke(
        InvokeRequest(
            request_id="request_capture",
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "summarize this"},
        )
    )

    assert result.status == "completed"
    assert len(capture.calls) == 1
    assert structured.run_events == [("create", "running"), ("complete", "completed")]
    assert len(structured.results) == 1


async def test_database_reconciler_recovers_plan_run_result_and_turn_after_restart(
    tmp_path,
    settings,
    registry_service,
) -> None:
    integration_settings = _settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'formation-reconcile.db'}",
    )
    await create_all_tables(integration_settings)
    session_factory = create_session_factory(integration_settings)
    runs = DatabaseRunRepository(session_factory)
    results = DatabaseResultRepository(session_factory)
    plans = DatabasePlanRepository(session_factory)
    failed = FailingStructuredSink()
    plan_service = PlanService(plans, structured_formation=failed)
    await plan_service.save_plan(_plan(plan_id="plan_reconcile"))
    capture = InspectingCapture(runs, results, fail=True)
    service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        invokers=build_default_invoker_registry(settings),
        turn_capture=capture,
        structured_formation=failed,
    )
    response = await service.invoke(
        InvokeRequest(
            request_id="request_reconcile",
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "recover durable formation"},
        )
    )
    assert response.status == "completed"
    assert len(await plans.list_formation_pending()) == 1
    assert len(await runs.list_formation_pending()) == 1
    assert len(await results.list_formation_pending()) == 1

    formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
    publisher = StructuredFormationPublisher(
        settings=integration_settings,
        repository=formation,
    )
    turn_capture = TurnCaptureService(
        settings=integration_settings,
        builder=TurnCapsuleBuilder(integration_settings),
        coordinator=FormationTriggerCoordinator(
            settings=integration_settings,
            repository=formation,
        ),
        event_repository=MemoryItemRepository(),
    )
    restarted = FormationReconciler(
        plan_repository=DatabasePlanRepository(session_factory),
        run_repository=DatabaseRunRepository(session_factory),
        result_repository=DatabaseResultRepository(session_factory),
        publisher=publisher,
        turn_capture=turn_capture,
    )
    first = await restarted.run_once()
    second = await restarted.run_once()

    assert first == {"plans": 1, "runs": 1, "results": 1, "turns": 1}
    assert second == {"plans": 0, "runs": 0, "results": 0, "turns": 0}
    pending_turns = await formation.list_pending_turns(
        tenant_id="t1", user_id="u1", session_id="s1"
    )
    assert len(pending_turns) == 1
    assert pending_turns[0].used_memory_ids == []
    await session_factory.kw["bind"].dispose()


async def test_direct_and_route_invocation_persist_real_turns_and_structured_jobs(
    settings,
    registry_service,
) -> None:
    integration_settings = _settings(memory_formation_window_turns=5)
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    capture = TurnCaptureService(
        settings=integration_settings,
        builder=TurnCapsuleBuilder(integration_settings),
        coordinator=FormationTriggerCoordinator(
            settings=integration_settings,
            repository=formation,
        ),
        event_repository=memories,
    )
    publisher = StructuredFormationPublisher(
        settings=integration_settings,
        repository=formation,
    )
    service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        invokers=build_default_invoker_registry(settings),
        turn_capture=capture,
        structured_formation=publisher,
    )
    user = {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}}

    direct = await service.invoke(
        InvokeRequest(
            request_id="request_direct_capture",
            session_id="s1",
            agent_id="summarizer",
            user=user,
            input={"text": "direct invocation"},
        )
    )
    routed = await service.invoke_from_route(
        RouteRequest.model_validate(
            {
                "request_id": "request_route_capture",
                "session_id": "s1",
                "user": user,
                "input": {"text": "route invocation"},
            }
        ),
        RouteResponse(
            request_id="request_route_capture",
            session_id="s1",
            decision=RouteDecision(
                action="open_agent",
                target_agent_id="summarizer",
                message="route",
            ),
            context=RouteContext(candidate_agent_ids=["summarizer"]),
            invocation=InvocationPreview(
                agent_id="summarizer",
                input={"text": "route invocation"},
            ),
        ),
    )

    assert direct.status == "completed"
    assert routed is not None and routed.status == "completed"
    pending = await formation.list_pending_turns(tenant_id="t1", user_id="u1", session_id="s1")
    assert [turn.request_id for turn in pending] == [
        "request_direct_capture",
        "request_route_capture",
    ]
    assert {job.trigger.value for job in formation.jobs.values()} == {"structured_event"}
    assert len(formation.jobs) == 6
    assert await memories.list_active(tenant_id="t1", user_id="u1", limit=10) == []


class FailingMockInvoker:
    async def invoke(self, definition, invocation):
        raise RuntimeError("agent failed")


async def test_failed_invocation_is_captured_but_does_not_create_long_term_memory(
    registry_service,
) -> None:
    settings = _settings(memory_formation_window_turns=5)
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    registry = AgentInvokerRegistry()
    registry.register("mock", FailingMockInvoker())
    service = InvocationService(
        registry=registry_service,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        invokers=registry,
        turn_capture=TurnCaptureService(
            settings=settings,
            builder=TurnCapsuleBuilder(settings),
            coordinator=FormationTriggerCoordinator(settings=settings, repository=formation),
            event_repository=memories,
        ),
        structured_formation=StructuredFormationPublisher(
            settings=settings,
            repository=formation,
        ),
    )

    result = await service.invoke(
        InvokeRequest(
            request_id="request_failed",
            session_id="failed_session",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "do not infer from the error"},
        )
    )

    assert result.status == "failed"
    turns = await formation.list_pending_turns(
        tenant_id="t1", user_id="u1", session_id="failed_session"
    )
    assert len(turns) == 1 and turns[0].result_status == "failed"
    assert await memories.list_active(tenant_id="t1", user_id="u1", limit=10) == []


async def test_route_only_and_feature_off_create_no_formation_side_effects(
    settings,
    registry_service,
) -> None:
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    off = _settings(memory_mode="off")
    service = InvocationService(
        registry=registry_service,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        invokers=build_default_invoker_registry(settings),
        turn_capture=TurnCaptureService(
            settings=off,
            builder=TurnCapsuleBuilder(off),
            coordinator=FormationTriggerCoordinator(settings=off, repository=formation),
            event_repository=memories,
        ),
        structured_formation=StructuredFormationPublisher(
            settings=off,
            repository=formation,
        ),
    )
    request = RouteRequest.model_validate(
        {
            "request_id": "request_route_only",
            "session_id": "route_only_session",
            "user": {"id": "u1", "attributes": {"tenant_id": "t1"}},
            "input": {"text": "needs clarification"},
        }
    )
    route_only = RouteResponse(
        request_id="request_route_only",
        session_id="route_only_session",
        decision=RouteDecision(action="clarify", message="more details"),
        context=RouteContext(candidate_agent_ids=["summarizer"]),
    )

    assert await service.invoke_from_route(request, route_only) is None

    with_invocation = route_only.model_copy(
        update={
            "decision": RouteDecision(
                action="open_agent",
                target_agent_id="summarizer",
                message="route",
            ),
            "invocation": InvocationPreview(
                agent_id="summarizer",
                input={"text": "now complete"},
            ),
        }
    )
    result = await service.invoke_from_route(request, with_invocation)
    assert result is not None and result.status == "completed"
    assert formation.turns == {}
    assert formation.jobs == {}
    assert memories.events == []


async def test_production_off_records_are_not_backfilled_after_enable(
    settings,
    registry_service,
) -> None:
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    plans = MemoryPlanRepository()
    await PlanService(
        plans,
        structured_formation=None,
        runtime_policy=build_memory_runtime_policy("off"),
    ).save_plan(_plan(plan_id="plan_created_off"))
    off_service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        invokers=build_default_invoker_registry(settings),
        turn_capture=None,
        structured_formation=None,
        runtime_policy=build_memory_runtime_policy("off"),
    )
    response = await off_service.invoke(
        InvokeRequest(
            request_id="request_created_off",
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "do not backfill"},
        )
    )
    assert response.status == "completed"

    enabled = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    reconciler = FormationReconciler(
        plan_repository=plans,
        run_repository=runs,
        result_repository=results,
        publisher=StructuredFormationPublisher(settings=enabled, repository=formation),
        turn_capture=TurnCaptureService(
            settings=enabled,
            builder=TurnCapsuleBuilder(enabled),
            coordinator=FormationTriggerCoordinator(settings=enabled, repository=formation),
            event_repository=memories,
        ),
    )
    assert await reconciler.run_once() == {
        "plans": 0,
        "runs": 0,
        "results": 0,
        "turns": 0,
    }
    assert formation.jobs == {} and formation.turns == {} and memories.events == []


async def test_private_skip_trace_is_reconciled_without_structured_jobs(
    settings,
    registry_service,
) -> None:
    enabled = _settings()
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        invokers=build_default_invoker_registry(settings),
        turn_capture=InspectingCapture(runs, results, fail=True),
        structured_formation=StructuredFormationPublisher(
            settings=enabled,
            repository=formation,
        ),
        runtime_policy=build_memory_runtime_policy("on"),
    )
    response = await service.invoke(
        InvokeRequest(
            request_id="private_skip_retry",
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "private"},
            context={"memory_policy": {"mode": "private"}},
        )
    )
    assert response.status == "completed"
    assert len(await results.list_formation_pending()) == 1
    reconciler = FormationReconciler(
        plan_repository=MemoryPlanRepository(),
        run_repository=runs,
        result_repository=results,
        publisher=StructuredFormationPublisher(settings=enabled, repository=formation),
        turn_capture=TurnCaptureService(
            settings=enabled,
            builder=TurnCapsuleBuilder(enabled),
            coordinator=FormationTriggerCoordinator(settings=enabled, repository=formation),
            event_repository=memories,
        ),
    )
    counts = await reconciler.run_once()
    assert counts == {"plans": 0, "runs": 0, "results": 0, "turns": 1}
    assert [event.event_type for event in memories.events] == ["formation_skipped"]
    assert await results.list_formation_pending() == []
    assert formation.jobs == {} and formation.turns == {}


@pytest.mark.parametrize("backend", ["memory", "database"])
async def test_private_skip_event_is_idempotent_when_marker_fails(
    backend,
    tmp_path,
    settings,
    registry_service,
) -> None:
    enabled = _settings()
    session_factory = None
    if backend == "memory":
        runs = MemoryRunRepository()
        results = FailOnceTurnMarkerMemoryResultRepository()
        plans = MemoryPlanRepository()
        formation = MemoryFormationTurnJobRepository()
        memories = MemoryItemRepository()
    else:
        enabled = _settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'private-skip-idempotency.db'}",
        )
        await create_all_tables(enabled)
        session_factory = create_session_factory(enabled)
        runs = DatabaseRunRepository(session_factory)
        results = FailOnceTurnMarkerDatabaseResultRepository(session_factory)
        plans = DatabasePlanRepository(session_factory)
        formation = DatabaseMemoryFormationTurnJobRepository(session_factory)
        memories = DatabaseMemoryItemRepository(session_factory)
    publisher = StructuredFormationPublisher(settings=enabled, repository=formation)
    capture = TurnCaptureService(
        settings=enabled,
        builder=TurnCapsuleBuilder(enabled),
        coordinator=FormationTriggerCoordinator(settings=enabled, repository=formation),
        event_repository=memories,
    )
    service = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        invokers=build_default_invoker_registry(settings),
        turn_capture=capture,
        structured_formation=publisher,
        runtime_policy=build_memory_runtime_policy("on"),
    )
    response = await service.invoke(
        InvokeRequest(
            request_id=f"private_marker_{backend}",
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "private"},
            context={"memory_policy": {"mode": "private"}},
        )
    )
    assert response.status == "completed"
    assert len(await results.list_formation_pending()) == 1
    assert len(await memories.list_events(tenant_id="t1", user_id="u1")) == 1
    reconciler = FormationReconciler(
        plan_repository=plans,
        run_repository=runs,
        result_repository=results,
        publisher=publisher,
        turn_capture=capture,
    )
    assert (await reconciler.run_once())["turns"] == 1
    assert len(await memories.list_events(tenant_id="t1", user_id="u1")) == 1
    assert await results.list_formation_pending() == []
    if session_factory is not None:
        await session_factory.kw["bind"].dispose()


async def test_private_plan_claim_suppression_is_atomic() -> None:
    repository = MemoryPlanRepository()
    off_service = PlanService(
        repository,
        runtime_policy=build_memory_runtime_policy("off"),
    )
    await off_service.save_plan(_plan(plan_id="private_plan_claim"))
    service = PlanService(
        repository,
        structured_formation=CapturingStructuredSink(),
        runtime_policy=build_memory_runtime_policy("on"),
    )
    claim = await service.claim_step(
        "private_plan_claim",
        "step_1",
        tenant_id="t1",
        user_id="u1",
        publish=False,
    )
    assert claim is not None
    assert await repository.list_formation_pending() == []


async def test_private_plan_executor_non_invocation_transitions_are_suppressed() -> None:
    settings = _settings()
    formation = MemoryFormationTurnJobRepository()
    publisher = StructuredFormationPublisher(settings=settings, repository=formation)
    repository = MemoryPlanRepository()
    service = PlanService(repository, structured_formation=publisher)
    await service.save_plan(_plan(plan_id="private_missing_agent"))
    initial_jobs = len(formation.jobs)

    class MissingRegistry:
        async def get_definition(self, agent_id):
            return None

    class InvocationStub:
        invokers = AvailableInvokers()

    executor = PlanExecutor(
        plan_service=service,
        registry=MissingRegistry(),
        invocation_service=InvocationStub(),
    )
    response = await executor.execute(
        "private_missing_agent",
        user={"id": "u1", "attributes": {"tenant_id": "t1"}},
        context={"memory_policy": {"mode": "private"}},
    )
    assert response.plan.status == "blocked"
    assert len(formation.jobs) == initial_jobs
    assert await repository.list_formation_pending() == []

    await service.save_plan(_plan(plan_id="private_confirm"))
    jobs_before_confirm = len(formation.jobs)
    await service.confirm(
        "private_confirm",
        tenant_id="t1",
        user_id="u1",
        publish=False,
    )
    assert len(formation.jobs) == jobs_before_confirm
    assert await repository.list_formation_pending() == []


class AvailableInvokers:
    def has(self, _invoker_type: str) -> bool:
        return True


class CancellingInvocationService:
    def __init__(self, plan_service: PlanService) -> None:
        self.plan_service = plan_service
        self.invokers = AvailableInvokers()

    async def invoke_agent(self, **kwargs) -> AgentInvocationResult:
        plan_id = kwargs["context"]["plan_id"]
        await self.plan_service.cancel(plan_id, tenant_id="t1", user_id="u1")
        return AgentInvocationResult(
            run_id="run_cancelled_during_execution",
            agent_id=kwargs["agent_id"],
            status="completed",
            message="stale completion",
        )


async def test_plan_executor_reloads_canonical_plan_before_persisting_step_result(
    registry_service,
) -> None:
    plan_service = PlanService(MemoryPlanRepository())
    plan = _plan(status="running")
    await plan_service.save_plan(plan)
    executor = PlanExecutor(
        plan_service=plan_service,
        registry=registry_service,
        invocation_service=CancellingInvocationService(plan_service),
    )

    response = await executor.execute(
        plan.plan_id,
        user={"id": "u1", "attributes": {"tenant_id": "t1"}},
        input_values={"text": "continue"},
    )

    assert response.plan.status == "cancelled"
    stored = await plan_service.get_plan(plan.plan_id, tenant_id="t1", user_id="u1")
    assert stored is not None and stored.status == "cancelled"
    assert stored.steps[0].status == "cancelled"


@pytest.mark.parametrize(
    "text",
    [
        "不要继续上次任务，改做一个新任务",
        "继续创建一个全新的项目",
        "别恢复之前的计划",
        "do not resume the previous task",
    ],
)
def test_continuation_classifier_rejects_negated_or_new_tasks(text: str) -> None:
    assert not requests_plan_continuation(text)


class InvocationCapturingInvoker:
    def __init__(self, *, status: str = "completed") -> None:
        self.status = status
        self.invocations = []

    async def invoke(self, definition, invocation):
        self.invocations.append(invocation)
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status=self.status,
            message=self.status,
        )


async def test_agent_task_memory_requires_matching_active_canonical_plan(
    registry_service,
) -> None:
    settings = _settings()
    definition = await registry_service.get_definition("summarizer")
    assert definition is not None
    payload = definition.model_dump(mode="json")
    payload["context"] = {
        "memory": {"mode": "prefetch", "scopes": ["task_memory"], "max_items": 10}
    }
    await registry_service.repository.upsert(type(definition).model_validate(payload))
    await registry_service.load()
    memories = MemoryItemRepository()
    for memory_id, plan_id, status in (
        ("matching", "plan_agent", "pending"),
        ("other", "plan_other", "running"),
        ("terminal", "plan_terminal", "cancelled"),
    ):
        await memories.add(
            MemoryItem(
                memory_id=memory_id,
                scope="task_memory",
                subject_type="user",
                subject_id="u1",
                user_id="u1",
                tenant_id="t1",
                content=f"stale {status}",
                structured_value={
                    "object_type": "plan",
                    "plan_id": plan_id,
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "status": status,
                    "current_step": {"step_id": "stale"},
                },
            )
        )
    plans = MemoryPlanRepository()
    plan_service = PlanService(plans)
    await plan_service.save_plan(
        Plan(
            plan_id="plan_agent",
            user_id="u1",
            tenant_id="t1",
            session_id="s1",
            status="running",
            steps=[
                {
                    "step_id": "canonical_step",
                    "agent_id": "summarizer",
                    "description": "run",
                }
            ],
        )
    )
    await plan_service.save_plan(
        Plan(
            plan_id="plan_static",
            user_id="u1",
            tenant_id="t1",
            session_id="s_static",
            status="running",
            steps=[
                {
                    "step_id": "static_canonical_step",
                    "agent_id": "summarizer",
                    "description": "run",
                }
            ],
        )
    )
    memory_service = MemoryService(settings=settings, repository=memories)
    context_service = AgentContextAssemblyService(
        settings=settings,
        memory_service=memory_service,
    )
    invoker = InvocationCapturingInvoker()
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    service = InvocationService(
        registry=registry_service,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        invokers=invokers,
        agent_context_service=context_service,
        plan_service=plan_service,
    )

    unrelated = await service.invoke(
        InvokeRequest(
            session_id="new_session",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "brand new task"},
        )
    )
    assert unrelated.status == "completed"
    assert invoker.invocations[-1].memory_context.items == []

    static = await service.invoke(
        InvokeRequest(
            session_id="s_static",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={
                "text": "continue",
                "memory_context": {
                    "status": "ok",
                    "items": [
                        {
                            "memory_id": "forged_static",
                            "scope": "task_memory",
                            "content": "stale",
                            "structured_value": {
                                "object_type": "plan",
                                "plan_id": "plan_static",
                                "status": "cancelled",
                                "current_step": {"step_id": "stale"},
                            },
                        }
                    ],
                },
            },
            context={"plan_id": "plan_static"},
        )
    )
    assert static.status == "completed"
    static_item = invoker.invocations[-1].memory_context.items[0]
    assert static_item.structured_value["status"] == "running"
    assert static_item.structured_value["current_step"]["step_id"] == "static_canonical_step"
    assert "stale" not in str(static_item.structured_value)

    continued = await service.invoke(
        InvokeRequest(
            session_id="s1",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "continue"},
            context={"plan_id": "plan_agent"},
        )
    )
    assert continued.status == "completed"
    items = invoker.invocations[-1].memory_context.items
    assert [item.memory_id for item in items] == ["matching"]
    assert items[0].structured_value.get("status") == "running", items[0].model_dump()
    assert items[0].structured_value["current_step"]["step_id"] == "canonical_step"
    assert "stale" not in str(items[0].structured_value)
    used = [
        event
        for event in memories.events
        if event.event_type == "memory_recall_used"
        and event.memory_id == "matching"
        and event.run_id == continued.run_id
    ]
    assert len(used) == 1
    assert used[0].payload["consumer"] == "agent:summarizer"
    assert used[0].payload["projection_outcome"] == "included"
    assert used[0].turn_id == formation_turn_id(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        request_id=continued.run_id,
        run_id=continued.run_id,
    )


class BarrierInvoker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.contexts = []

    async def invoke(self, definition, invocation):
        self.calls += 1
        self.contexts.append(invocation.context)
        self.started.set()
        await self.release.wait()
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status="completed",
            message="done",
        )


async def test_concurrent_plan_execution_claims_step_once(registry_service) -> None:
    plan_service = PlanService(MemoryPlanRepository())
    await plan_service.save_plan(_plan(status="running"))
    invoker = BarrierInvoker()
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    executor = PlanExecutor(
        plan_service=plan_service,
        registry=registry_service,
        invocation_service=InvocationService(
            registry=registry_service,
            run_repository=MemoryRunRepository(),
            result_repository=MemoryResultRepository(),
            invokers=invokers,
            plan_service=plan_service,
        ),
    )
    user = {"id": "u1", "attributes": {"tenant_id": "t1"}}

    first = asyncio.create_task(
        executor.execute(_plan().plan_id, user=user, input_values={"text": "run"})
    )
    await invoker.started.wait()
    second = await executor.execute(
        _plan().plan_id,
        user=user,
        input_values={"text": "run"},
    )
    assert invoker.calls == 1
    assert second.results == [] and second.plan.status == "running"
    invoker.release.set()
    completed = await first
    assert completed.plan.status == "completed"
    assert invoker.calls == 1


async def test_plan_claim_heartbeat_prevents_reclaim_during_long_agent_call(
    registry_service,
) -> None:
    plan_service = PlanService(MemoryPlanRepository())
    await plan_service.save_plan(_plan(status="running"))
    invoker = BarrierInvoker()
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    executor = PlanExecutor(
        plan_service=plan_service,
        registry=registry_service,
        invocation_service=InvocationService(
            registry=registry_service,
            run_repository=MemoryRunRepository(),
            result_repository=MemoryResultRepository(),
            invokers=invokers,
            plan_service=plan_service,
            plan_claim_lease_seconds=0.06,
        ),
    )
    user = {"id": "u1", "attributes": {"tenant_id": "t1"}}
    first = asyncio.create_task(
        executor.execute(_plan().plan_id, user=user, input_values={"text": "run"})
    )
    await invoker.started.wait()
    await asyncio.sleep(0.15)
    second = await executor.execute(
        _plan().plan_id,
        user=user,
        input_values={"text": "run"},
    )
    assert invoker.calls == 1
    assert second.results == [] and second.plan.status == "running"
    assert invoker.contexts[0]["plan_execution_idempotency_key"].startswith("plan_exec_")
    invoker.release.set()
    completed = await first
    assert completed.plan.status == "completed"


async def test_local_function_deduplicates_same_plan_execution_key(registry_service) -> None:
    definition = await registry_service.get_definition("summarizer")
    assert definition is not None
    payload = definition.model_dump(mode="json")
    payload["type"] = "local_function"
    payload["invocation"] = {
        "type": "local_function",
        "config": {"function": "side_effect"},
    }
    definition = type(definition).model_validate(payload)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def side_effect(invocation):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return {
            "status": "completed",
            "output": {"key": invocation.context["plan_execution_idempotency_key"]},
        }

    registry = LocalFunctionRegistry()
    registry.register("side_effect", side_effect)
    invoker = LocalFunctionInvoker(registry)

    def invocation(run_id: str) -> AgentInvocation:
        return AgentInvocation(
            run_id=run_id,
            session_id="s1",
            agent_id=definition.agent_id,
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "run"},
            context={"plan_execution_idempotency_key": "plan_exec_same"},
        )

    first = asyncio.create_task(invoker.invoke(definition, invocation("run_1")))
    await asyncio.to_thread(started.wait, 1)
    second = asyncio.create_task(invoker.invoke(definition, invocation("run_2")))
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert calls == 1
    assert first_result.run_id == "run_1" and second_result.run_id == "run_2"
    assert first_result.output == second_result.output == {"key": "plan_exec_same"}


async def test_request_private_suppresses_structured_jobs_but_records_skip(
    registry_service,
) -> None:
    settings = _settings(memory_formation_window_turns=1)
    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    service = InvocationService(
        registry=registry_service,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        invokers=build_default_invoker_registry(settings),
        turn_capture=TurnCaptureService(
            settings=settings,
            builder=TurnCapsuleBuilder(settings),
            coordinator=FormationTriggerCoordinator(settings=settings, repository=formation),
            event_repository=memories,
        ),
        structured_formation=StructuredFormationPublisher(
            settings=settings,
            repository=formation,
        ),
    )
    result = await service.invoke(
        InvokeRequest(
            session_id="private_session",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "private request"},
            context={"memory_policy": {"mode": "private"}},
        )
    )

    assert result.status == "completed"
    assert formation.jobs == {} and formation.turns == {}
    assert [event.event_type for event in memories.events] == ["formation_skipped"]


async def test_blocked_invocation_publishes_run_update_not_fail(registry_service) -> None:
    sink = CapturingStructuredSink()
    invoker = InvocationCapturingInvoker(status="blocked")
    invokers = AgentInvokerRegistry()
    invokers.register("mock", invoker)
    service = InvocationService(
        registry=registry_service,
        run_repository=MemoryRunRepository(),
        result_repository=MemoryResultRepository(),
        invokers=invokers,
        structured_formation=sink,
    )
    result = await service.invoke(
        InvokeRequest(
            session_id="blocked_session",
            agent_id="summarizer",
            user={"id": "u1", "attributes": {"tenant_id": "t1"}},
            input={"text": "block"},
        )
    )
    assert result.status == "blocked"
    assert sink.run_events == [("create", "running"), ("update", "blocked")]
