from app.core.config import Settings
from app.schemas.context import ContextBudget, ContextCandidate
from app.schemas.routing import (
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.services.context_pipeline_service import ContextPipelineService
from app.services.router_service import RouterService


def _request() -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "request_trace",
            "session_id": "session_trace",
            "user": {"id": "user_trace", "roles": ["operator"]},
            "input": {"text": "summarize"},
        }
    )


async def test_trace_records_identity_provider_governance_budget_and_versions() -> None:
    pipeline = ContextPipelineService(
        Settings(
            context_pipeline_mode="enforced",
            context_policy_version="policy-replay",
            context_budget_version="budget-replay",
            context_projection_version="projection-replay",
        )
    )
    candidates = [
        ContextCandidate(
            candidate_id="current_input",
            source="current_input",
            scope="request",
            content="summarize",
            purpose="route_decision",
            consumers=["router"],
            authority="authoritative",
            must_include=True,
        ),
        ContextCandidate(
            candidate_id="duplicate",
            source="memory",
            scope="user",
            content="summarize",
            purpose="route_decision",
            consumers=["router"],
            authority="derived",
        ),
    ]

    first = await pipeline.assemble_candidates(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        candidates=candidates,
        budget=ContextBudget(max_tokens=500),
    )
    second = await pipeline.assemble_candidates(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        candidates=candidates,
        budget=ContextBudget(max_tokens=500),
    )

    assert first.pack.trace_id == first.trace.trace_id
    assert first.pack.pack_id == first.trace.pack_id == first.projection.pack_id
    assert first.trace.purpose == "route_decision"
    assert first.trace.consumer == "router"
    assert first.trace.policy_version == "policy-replay"
    assert first.trace.budget_version == "budget-replay"
    assert first.trace.projection_version == "projection-replay"
    assert any(item.outcome == "duplicate_dropped" for item in first.trace.decisions)
    assert first.trace.budget and first.trace.budget.used_tokens <= 500
    assert first.projection.projection_hash == second.projection.projection_hash


async def test_route_log_keeps_bounded_trace_without_prompt_or_raw_values(
    settings,
    registry_service,
    repositories,
) -> None:
    configured = settings.model_copy(update={"context_pipeline_mode": "enforced"})
    service = RouterService(
        settings=configured,
        registry=registry_service,
        llm_client=TraceLLM(),
        route_log_repository=repositories["route_logs"],
    )
    request = _request().model_copy(
        update={
            "frontend_context": {
                "page": "inbox",
                "token": "secret-token",
                "raw": "raw-value" * 1000,
            }
        }
    )

    await service.route(request)

    route_log = repositories["route_logs"].logs[-1]
    stored = str(route_log.parsed_output)
    summary = route_log.parsed_output["context_pack_usage"]
    assert summary["purpose"] == "route_decision"
    assert summary["consumer"] == "router"
    assert summary["trace_id"]
    assert summary["projection_hash"]
    assert "secret-token" not in stored
    assert "raw-value" * 20 not in stored
    assert "payload_json" not in stored
    assert "projection.payload" not in stored


async def test_trace_persistence_failure_does_not_block_route_and_is_observable(
    settings,
    registry_service,
) -> None:
    configured = settings.model_copy(update={"context_pipeline_mode": "enforced"})
    service = RouterService(
        settings=configured,
        registry=registry_service,
        llm_client=TraceLLM(),
        route_log_repository=FailingRouteLogRepository(),
    )

    response = await service.route(_request())

    persistence = response.context.metadata["context_trace"]["persistence"]
    assert persistence["status"] == "error"
    assert persistence["error_code"] == "route_log_persistence_failed"


class TraceLLM:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        target = payload.candidates[0].agent_id
        return RouteResponse(
            request_id=payload.request.request_id or "request_trace",
            session_id=payload.request.session_id,
            assistant_message="routed",
            decision=RouteDecision(
                action="open_agent",
                target_agent_id=target,
                confidence=0.9,
                message="routed",
            ),
            context=RouteContext(candidate_agent_ids=[target]),
        )


class FailingRouteLogRepository:
    async def add(self, log):
        raise RuntimeError("storage unavailable")
