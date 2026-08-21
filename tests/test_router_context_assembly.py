from app.core.config import Settings
from app.core.memory_runtime import build_memory_runtime_policy
from app.plugins.evidence import EvidenceResult
from app.repositories.database import DatabaseEventRepository, DatabasePlanRepository
from app.repositories.memory import MemoryEventRepository, MemoryPlanRepository
from app.schemas.events import AgentEvent
from app.schemas.logs import AgentResult
from app.schemas.plans import Plan
from app.schemas.routing import (
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.schemas.sessions import AppendChatMessageRequest
from app.services.chat_history_service import ChatHistoryService
from app.services.event_service import EventService
from app.services.plan_service import PlanService
from app.services.router_service import RouterService


async def test_event_repository_gets_referenced_and_bounded_recent_events() -> None:
    repository = MemoryEventRepository()
    service = EventService(repository)
    for index in range(4):
        await service.record_agent_event(
            AgentEvent(
                event_id=f"event_{index}",
                session_id="session_1",
                agent_id="summarizer",
                user_id="u1",
                tenant_id="t1",
                event_type="agent_progress",
                payload={"index": index},
            )
        )

    referenced = await service.get_event("event_1", tenant_id="t1", user_id="u1")
    recent = await service.list_recent_events("session_1", tenant_id="t1", user_id="u1", limit=2)

    assert referenced and referenced.event_id == "event_1"
    assert [event.event_id for event in recent] == ["event_3", "event_2"]


async def test_plan_repository_finds_latest_active_plan_by_session() -> None:
    repository = MemoryPlanRepository()
    service = PlanService(repository)
    for plan_id, status in (("completed", "completed"), ("active", "blocked")):
        await service.save_plan(
            Plan.model_validate(
                {
                    "plan_id": plan_id,
                    "user_id": "u1",
                    "tenant_id": "t1",
                    "session_id": "session_1",
                    "status": status,
                    "steps": [
                        {
                            "step_id": f"step_{plan_id}",
                            "agent_id": "summarizer",
                            "description": plan_id,
                            "status": status,
                        }
                    ],
                }
            )
        )

    active = await service.get_active_plan("session_1", tenant_id="t1", user_id="u1")

    assert active and active.plan_id == "active"


async def test_router_loads_referenced_recent_event_and_session_active_plan(
    settings,
    registry_service,
    repositories,
) -> None:
    await repositories["events"].add_agent_event(
        AgentEvent(
            event_id="event_ref",
            session_id="session_1",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            event_type="agent_progress",
            status="running",
            payload={"progress": 50},
        )
    )
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "plan_active",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "session_1",
                "status": "running",
                "steps": [
                    {
                        "step_id": "step_1",
                        "agent_id": "summarizer",
                        "description": "summarize",
                        "status": "running",
                    }
                ],
            }
        )
    )
    service = RouterService(
        settings=settings.model_copy(update={"context_pipeline_mode": "observe"}),
        registry=registry_service,
        event_service=EventService(repositories["events"]),
        plan_service=PlanService(repositories["plans"]),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "session_1",
                "source": "agent_event",
                "event_id": "event_ref",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "what next"},
            }
        )
    )

    debug = response.context.metadata["context_pack"]
    included_sources = {item["source"] for item in debug["selection"] if item["included"]}
    assert "recent_event" in included_sources
    assert "current_plan" in included_sources


async def test_database_event_and_plan_queries_match_memory_behavior(
    tmp_path, managed_database
) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'context-state.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    event_service = EventService(DatabaseEventRepository(session_factory))
    plan_service = PlanService(DatabasePlanRepository(session_factory))
    for index in range(3):
        await event_service.record_agent_event(
            AgentEvent(
                event_id=f"db_event_{index}",
                session_id="db_session",
                agent_id="summarizer",
                user_id="u1",
                tenant_id="t1",
                event_type="agent_progress",
                payload={"index": index},
            )
        )
    await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "db_completed",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "db_session",
                "status": "completed",
                "steps": [
                    {
                        "step_id": "done",
                        "agent_id": "summarizer",
                        "description": "done",
                        "status": "completed",
                    }
                ],
            }
        )
    )
    await plan_service.save_plan(
        Plan.model_validate(
            {
                "plan_id": "db_active",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "db_session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "running",
                        "agent_id": "summarizer",
                        "description": "running",
                        "status": "running",
                    }
                ],
            }
        )
    )

    referenced = await event_service.get_event("db_event_1", tenant_id="t1", user_id="u1")
    recent = await event_service.list_recent_events(
        "db_session", tenant_id="t1", user_id="u1", limit=2
    )
    active = await plan_service.get_active_plan("db_session", tenant_id="t1", user_id="u1")

    assert referenced and referenced.event_id == "db_event_1"
    assert len(recent) == 2
    assert {event.event_id for event in recent} <= {"db_event_0", "db_event_1", "db_event_2"}
    assert active and active.plan_id == "db_active"


async def test_enforced_router_projection_contains_all_governed_backend_sources(
    settings,
    registry_service,
    repositories,
) -> None:
    chat = ChatHistoryService(repositories["messages"], host_limit=20, agent_limit=12)
    await chat.append_message(
        session_id="full_session",
        payload=AppendChatMessageRequest(
            source="host_chat",
            role="assistant",
            user_id="u1",
            tenant_id="t1",
            content="host history",
        ),
    )
    await chat.append_message(
        session_id="full_session",
        payload=AppendChatMessageRequest(
            source="agent_chat",
            role="agent",
            user_id="u1",
            tenant_id="t1",
            agent_id="summarizer",
            content="agent history",
        ),
    )
    await repositories["events"].add_agent_event(
        AgentEvent(
            event_id="full_event",
            session_id="full_session",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            event_type="agent_progress",
            status="running",
        )
    )
    await repositories["results"].add_result(
        AgentResult(
            result_id="full_result",
            run_id="full_run",
            session_id="full_session",
            agent_id="summarizer",
            user_id="u1",
            tenant_id="t1",
            status="completed",
            output={"summary": "result summary", "raw": "unbounded-result-secret" * 500},
            artifact_refs=[
                {"artifact_id": "artifact_1", "type": "text", "uri": "memory://artifact_1"},
                {"artifact_id": "artifact_2", "type": "text", "uri": "memory://artifact_2"},
            ],
        )
    )
    await repositories["plans"].save(
        Plan.model_validate(
            {
                "plan_id": "full_plan",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": "full_session",
                "status": "running",
                "steps": [
                    {
                        "step_id": "full_step",
                        "agent_id": "summarizer",
                        "description": "continue",
                        "status": "running",
                    }
                ],
            }
        )
    )
    llm = ProjectionCapturingLLM()
    service = RouterService(
        settings=settings.model_copy(update={"context_pipeline_mode": "enforced"}),
        registry=registry_service,
        llm_client=llm,
        chat_history_service=chat,
        result_repository=repositories["results"],
        event_service=EventService(repositories["events"]),
        plan_service=PlanService(repositories["plans"]),
        evidence_provider=StaticEvidenceProvider(),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "full_session",
                "source": "host_chat",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "continue"},
                "current_agent": {"agent_id": "summarizer"},
            }
        )
    )

    assert llm.calls == 1
    assert llm.payload and llm.payload.projection is not None
    sources = {item["source"] for item in llm.payload.projection.payload["context"]["items"]}
    assert sources >= {
        "current_input",
        "current_agent",
        "host_history",
        "agent_history",
        "recent_event",
        "current_plan",
        "recent_result",
        "artifact",
        "evidence",
    }
    assert "unbounded-result-secret" not in str(llm.payload.projection.payload)
    assert response.context.metadata["artifact_ambiguity"]["count"] == 2
    assert response.context.metadata["context_pipeline"]["mode"] == "enforced"


async def test_router_session_sources_are_isolated_by_tenant_and_user(
    settings,
    registry_service,
    repositories,
) -> None:
    chat = ChatHistoryService(repositories["messages"], host_limit=20, agent_limit=12)
    for user_id, tenant_id, content in (
        ("u1", "t1", "own-history"),
        ("u2", "t1", "other-user-secret"),
        ("u1", "t2", "other-tenant-secret"),
    ):
        await chat.append_message(
            session_id="shared_session",
            payload=AppendChatMessageRequest(
                source="host_chat",
                role="assistant",
                user_id=user_id,
                tenant_id=tenant_id,
                content=content,
            ),
        )
        await repositories["results"].add_result(
            AgentResult(
                result_id=f"result_{tenant_id}_{user_id}",
                run_id=f"run_{tenant_id}_{user_id}",
                session_id="shared_session",
                agent_id="summarizer",
                user_id=user_id,
                tenant_id=tenant_id,
                status="completed",
                output={"summary": content},
            )
        )
        await repositories["events"].add_agent_event(
            AgentEvent(
                event_id=f"event_{tenant_id}_{user_id}",
                session_id="shared_session",
                agent_id="summarizer",
                user_id=user_id,
                tenant_id=tenant_id,
                event_type="agent_progress",
                status="running",
                payload={"summary": content},
            )
        )
    llm = ProjectionCapturingLLM()
    service = RouterService(
        settings=settings.model_copy(update={"context_pipeline_mode": "enforced"}),
        registry=registry_service,
        llm_client=llm,
        chat_history_service=chat,
        result_repository=repositories["results"],
        event_service=EventService(repositories["events"]),
    )

    await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "shared_session",
                "user": {
                    "id": "u1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
                "input": {"text": "summarize"},
            }
        )
    )
    serialized = str(llm.payload.projection.payload)
    assert "own-history" in serialized
    assert "other-user-secret" not in serialized
    assert "other-tenant-secret" not in serialized


async def test_observe_mode_builds_projection_but_calls_router_llm_once(
    settings,
    registry_service,
) -> None:
    llm = ProjectionCapturingLLM()
    service = RouterService(
        settings=settings.model_copy(update={"context_pipeline_mode": "observe"}),
        registry=registry_service,
        llm_client=llm,
        runtime_policy=build_memory_runtime_policy("observe"),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "observe_session",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize"},
            }
        )
    )

    assert llm.calls == 1
    assert llm.payload and llm.payload.projection is None
    assert response.context.metadata["context_pipeline"]["mode"] == "observe"
    assert response.context.metadata["context_pipeline"]["legacy_input_hash"]
    assert response.context.metadata["context_pipeline"]["projection_hash"]


class ProjectionCapturingLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.payload: LLMRouteInput | None = None

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        self.calls += 1
        self.payload = payload
        target = payload.candidates[0].agent_id
        return RouteResponse(
            request_id=payload.request.request_id or "request_capture",
            session_id=payload.request.session_id,
            assistant_message="captured",
            decision=RouteDecision(
                action="continue_agent" if payload.request.current_agent else "open_agent",
                target_agent_id=target,
                confidence=0.9,
                message="captured",
            ),
            context=RouteContext(
                relation="continue_current" if payload.request.current_agent else "new_task",
                current_agent_id=target if payload.request.current_agent else None,
                candidate_agent_ids=[item.agent_id for item in payload.candidates],
            ),
        )


class StaticEvidenceProvider:
    async def match(self, *, question, candidate_agent_ids, user):
        return EvidenceResult(
            evidence=[
                {
                    "id": "evidence_full",
                    "content": "evidence content",
                    "score": 0.9,
                }
            ]
        )
