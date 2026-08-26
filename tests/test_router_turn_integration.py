import pytest

from app.core.config import Settings
from app.core.errors import LLMError
from app.core.memory_runtime import build_memory_runtime_policy
from app.llm.conversation_formation import (
    ConversationFormationResponse,
    FakeConversationFormationModel,
)
from app.repositories.canonical_invocations import MemoryCanonicalInvocationStore
from app.repositories.context_stores import MemoryItemRepository
from app.repositories.memory import MemoryResultRepository, MemoryRunRepository
from app.repositories.memory_formation import MemoryFormationTurnJobRepository
from app.repositories.turn_outbox import MemoryTurnOutboxRepository
from app.repositories.turns import MemoryTurnRepository
from app.schemas.common import UserContext
from app.schemas.memory import (
    MemoryCandidateSemantics,
    MemoryEvidenceRef,
    MemoryFormationCandidate,
    MemoryRecallRequest,
)
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
from app.services.invocation_service import InvocationService
from app.services.memory_adapter import (
    MemoryIndexOperationResult,
    MemoryProviderOperationStatus,
    RepositoryMemoryAdapter,
)
from app.services.memory_candidate_policy import MemoryCandidatePolicy
from app.services.memory_formation import (
    FormationJobWorker,
    FormationTriggerCoordinator,
    TurnCapsuleBuilder,
    TurnOutboxFormationConsumer,
)
from app.services.memory_indexing import MemoryIndexOperationWorker
from app.services.memory_integration import MemoryFormationProcessor
from app.services.memory_service import MemoryService
from app.services.plan_service import PlanService
from app.services.router_service import RouterService
from app.services.turn_service import TurnIdempotencyConflict, TurnService


async def test_router_creates_and_recovers_canonical_turn_without_host_history(
    settings, registry_service
) -> None:
    repository = MemoryTurnRepository()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        turn_service=TurnService(repository),
    )
    request = RouteRequest.model_validate(
        {
            "request_id": "request-turn-1",
            "session_id": "session-1",
            "user": {
                "id": "user-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            "input": {"text": "summarize current input", "attachments": [{"id": "a1"}]},
            "frontend_context": {
                "history": [{"role": "assistant", "content": "must not persist"}],
                "provider_session_id": "provider-session",
            },
        }
    )

    first = await service.route(request)
    second = await service.route(request)
    turn = await repository.get_by_request(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-turn-1",
    )

    assert first.request_id == second.request_id == "request-turn-1"
    assert turn is not None
    assert turn.user_input.text == "summarize current input"
    assert turn.user_input.metadata == {"input_type": "text", "attachment_count": 1}
    assert "history" not in turn.user_input.metadata
    assert "provider_session_id" not in turn.user_input.metadata


async def test_router_rejects_conflicting_semantic_retry(settings, registry_service) -> None:
    repository = MemoryTurnRepository()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        turn_service=TurnService(repository),
    )
    base = {
        "request_id": "request-conflict",
        "session_id": "session-1",
        "user": {"id": "user-1", "attributes": {"tenant_id": "tenant-1"}},
        "input": {"text": "original input"},
    }
    await service.route(RouteRequest.model_validate(base))

    with pytest.raises(TurnIdempotencyConflict, match="conflicts"):
        await service.route(
            RouteRequest.model_validate({**base, "input": {"text": "changed input"}})
        )


@pytest.mark.parametrize("action", ["reply", "clarify", "unsupported", "silent"])
async def test_router_completes_route_only_turn_without_fake_run(
    action, settings, registry_service
) -> None:
    repository = MemoryTurnRepository()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=_DirectRouteLLM(action),
        turn_service=TurnService(repository),
    )
    request = RouteRequest.model_validate(
        {
            "request_id": f"request-{action}",
            "session_id": "session-1",
            "user": {
                "id": "user-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            "input": {"text": f"request {action}"},
        }
    )

    response = await service.route(request)
    turn = await repository.get_by_request(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id=f"request-{action}",
    )

    assert response.decision.action == action
    assert turn is not None and turn.status.value == "completed"
    assert turn.final_response is not None and turn.final_response.kind == action
    assert turn.references.run_ids == []
    assert turn.references.result_ids == []


async def test_router_terminalizes_turn_when_route_provider_response_is_unrepairable(
    settings, registry_service
) -> None:
    repository = MemoryTurnRepository()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=_UnrepairableRouteLLM(),
        turn_service=TurnService(repository),
    )
    request = RouteRequest.model_validate(
        {
            "request_id": "request-unrepairable-route",
            "session_id": "session-1",
            "user": {
                "id": "user-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            "input": {"text": "first summarize this text, then create a task"},
        }
    )

    with pytest.raises(LLMError, match="does not match RouteResponse"):
        await service.route(request)

    turn = await repository.get_by_request(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-unrepairable-route",
    )

    assert turn is not None
    assert turn.status.value == "failed"
    assert turn.final_response is not None
    assert turn.final_response.kind == "error"
    assert turn.final_response.error == {"code": "llm_error"}


class _DirectRouteLLM:
    def __init__(self, action: str) -> None:
        self.action = action

    async def route(self, payload) -> RouteResponse:
        message = "" if self.action == "silent" else f"direct {self.action}"
        return RouteResponse(
            request_id=payload.request.request_id,
            session_id=payload.request.session_id,
            assistant_message=message or None,
            decision=RouteDecision(
                action=self.action,
                status="unsupported" if self.action == "unsupported" else "ok",
                reason="direct route test",
                message=message,
            ),
            context=RouteContext(),
        )


class _UnrepairableRouteLLM:
    async def route(self, _payload) -> RouteResponse:
        raise LLMError(
            "OpenAI-compatible LLM returned a response that does not match RouteResponse"
        )


async def test_router_binds_pending_plan_to_blocked_turn(
    settings, registry_service, repositories, task_creator_agent
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    repository = MemoryTurnRepository()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        plan_service=PlanService(repositories["plans"]),
        turn_service=TurnService(repository),
    )
    await service.route(
        RouteRequest.model_validate(
            {
                "request_id": "request-plan-turn",
                "session_id": "session-plan",
                "user": {
                    "id": "user-1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "tenant-1"},
                },
                "input": {"text": "first summarize this text, then create a task"},
            }
        )
    )
    turn = await repository.get_by_request(
        tenant_id="tenant-1", user_id="user-1", request_id="request-plan-turn"
    )

    assert turn is not None and turn.status.value == "blocked"
    assert turn.references.plan_id is not None
    assert turn.final_response is None


async def test_route_and_invoke_completes_canonical_turn_and_creates_outbox(
    settings, registry_service
) -> None:
    turns = MemoryTurnRepository()
    outbox = MemoryTurnOutboxRepository()
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    router = RouterService(
        settings=settings,
        registry=registry_service,
        turn_service=TurnService(turns),
    )
    invocation = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        canonical_invocation_store=MemoryCanonicalInvocationStore(
            run_repository=runs,
            result_repository=results,
            turn_repository=turns,
            outbox_repository=outbox,
        ),
        runtime_policy=build_memory_runtime_policy("on"),
    )
    request = RouteRequest.model_validate(
        {
            "request_id": "request-route-invoke",
            "session_id": "session-route-invoke",
            "user": {
                "id": "user-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            "input": {"text": "summarize this visit preparation"},
        }
    )

    route = await router.route(request)
    result = await invocation.invoke_from_route(request, route)
    turn = await turns.get_by_request(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-route-invoke",
    )

    assert result is not None and result.status == "completed"
    assert turn is not None and turn.status.value == "completed"
    assert len(turn.references.run_ids) == 1
    assert len(turn.references.result_ids) == 1
    assert len(outbox.events) == 1
    event = next(iter(outbox.events.values()))
    assert event.event_type == "turn.completed"
    assert event.payload["formation_eligibility"]["mode"] == "enforced"


class _ReadyRepositoryMemoryAdapter(RepositoryMemoryAdapter):
    async def execute_index_operation(self, operation, *, item):
        return MemoryIndexOperationResult(
            operation=operation.operation,
            status=MemoryProviderOperationStatus.SUCCESS,
            memory_id=operation.memory_id,
            external_memory_id=f"index:{operation.memory_id}",
        )


async def test_route_invoke_pork_preference_reaches_index_and_recall_after_restarts(
    registry_service,
) -> None:
    settings = Settings(
        storage_backend="memory",
        memory_strategy_provider="memory",
        memory_mode="on",
        memory_formation_window_turns=1,
        memory_formation_model_timeout_seconds=1,
    )
    turns = MemoryTurnRepository()
    outbox = MemoryTurnOutboxRepository()
    runs = MemoryRunRepository()
    results = MemoryResultRepository()
    router = RouterService(
        settings=settings,
        registry=registry_service,
        turn_service=TurnService(turns),
    )
    invocation = InvocationService(
        registry=registry_service,
        run_repository=runs,
        result_repository=results,
        canonical_invocation_store=MemoryCanonicalInvocationStore(
            run_repository=runs,
            result_repository=results,
            turn_repository=turns,
            outbox_repository=outbox,
        ),
        runtime_policy=build_memory_runtime_policy("on"),
    )
    request = RouteRequest.model_validate(
        {
            "request_id": "request-pork-preference-e2e",
            "session_id": "session-pork-preference-e2e",
            "user": {
                "id": "user-1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "tenant-1"},
            },
            "input": {"text": "明天要拜访一位关注稳健理财的客户，帮我做访前准备。我喜欢吃猪肉。"},
        }
    )

    route = await router.route(request)
    invocation_result = await invocation.invoke_from_route(request, route)
    canonical = await turns.get_by_request(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id=request.request_id,
    )
    assert invocation_result is not None and invocation_result.status == "completed"
    assert canonical is not None and canonical.status.value == "completed"

    formation = MemoryFormationTurnJobRepository()
    memories = MemoryItemRepository()
    adapter = _ReadyRepositoryMemoryAdapter(memories)
    memory = MemoryService(
        settings=settings,
        repository=memories,
        adapter=adapter,
        formation_repository=formation,
    )
    index_outbox = memory.index_outbox

    # A fresh consumer simulates restart after Turn completion but before Outbox consumption.
    consumer = TurnOutboxFormationConsumer(
        settings=settings,
        outbox_repository=outbox,
        turn_repository=turns,
        builder=TurnCapsuleBuilder(settings),
        coordinator=FormationTriggerCoordinator(settings=settings, repository=formation),
        event_repository=memories,
        owner="e2e-consumer-after-restart",
    )
    assert (await consumer.run_once())["status"] == "captured"
    assert len(formation.jobs) == 1
    candidate = MemoryFormationCandidate(
        candidate_id="candidate-pork-preference",
        proposed_operation="add",
        scope="user_preference",
        content="User likes eating pork.",
        structured_value={"slot": "food_preference", "value": "pork"},
        semantic=MemoryCandidateSemantics(
            target="assistant_response",
            slot="food_preference",
            value="pork",
            temporal_scope="long_term",
            polarity="affirmed",
            certainty="certain",
            change_intent="set",
        ),
        subject_id_hint="user-1",
        tenant_id_hint="tenant-1",
        memory_key_hint="food_preference",
        confidence=0.99,
        evidence_refs=[
            MemoryEvidenceRef(
                turn_id=canonical.turn_id,
                role="user",
                quote="我喜欢吃猪肉",
            )
        ],
        reason="explicit stable food preference",
    )
    processor = MemoryFormationProcessor(
        repository=formation,
        memory_repository=memories,
        model=FakeConversationFormationModel(ConversationFormationResponse(candidates=[candidate])),
        policy=MemoryCandidatePolicy(settings=settings, repository=memories),
        lifecycle=memory.lifecycle,
    )
    worker = FormationJobWorker(
        settings=settings,
        repository=formation,
        processor=processor,
        owner="e2e-formation-after-restart",
    )
    completed_job = await worker.run_once()
    assert completed_job is not None and completed_job.status.value == "completed"
    assert completed_job.trace_summary.get("operation_counts") == {"add": 1}

    # A fresh index worker resumes the committed canonical revision exactly once.
    index_worker = MemoryIndexOperationWorker(
        settings=settings,
        adapter=adapter,
        repository=memories,
        outbox=index_outbox,
        lifecycle_store=memory.lifecycle_store,
        owner="e2e-index-after-restart",
    )
    indexed = await index_worker.run_once()
    assert indexed is not None and indexed.completed is True
    assert await index_worker.run_once() is None

    formed = await memories.list_active(
        tenant_id="tenant-1",
        user_id="user-1",
        scopes=["user_preference"],
        limit=10,
    )
    assert len(formed) == 1
    assert formed[0].content == "User likes eating pork."
    assert formed[0].current_revision_id
    assert formed[0].index_status == "ready"
    assert canonical.turn_id in formed[0].canonical_refs

    recall = await memory.recall(
        MemoryRecallRequest(
            query="我喜欢吃什么？",
            user=UserContext(id="user-1", attributes={"tenant_id": "tenant-1"}),
            scopes=["user_preference"],
            metadata_filters={
                "request_id": "request-pork-recall",
                "session_id": "session-pork-recall",
                "consumer": "agent:visit-preparation",
            },
        )
    )
    assert [item.memory_id for item in recall.context.items] == [formed[0].memory_id]
    recall_event = next(
        event for event in memories.events if event.event_type == "memory_recall_used"
    )
    assert recall_event.request_id == "request-pork-recall"
    assert recall_event.memory_id == formed[0].memory_id
