import pytest

from app.repositories.turns import MemoryTurnRepository
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
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
