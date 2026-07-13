from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.schemas.context import ContextBudget, ContextCandidate
from app.schemas.routing import RouteRequest
from app.services.context_pipeline_service import ContextPipelineService


def _request() -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "request_policy",
            "session_id": "session_policy",
            "user": {"id": "user_policy"},
            "input": {"text": "current"},
        }
    )


async def test_backend_authority_supersedes_conflicting_host_assertion() -> None:
    result = await _assemble(
        [
            _candidate(
                "backend_plan",
                "current_plan",
                "completed",
                authority="authoritative",
                conflict_key="plan:1:status",
                fact_value="completed",
            ),
            _candidate(
                "frontend_plan",
                "frontend_context",
                "running",
                authority="host_asserted",
                conflict_key="plan:1:status",
                fact_value="running",
            ),
        ]
    )

    assert [item.item_id for item in result.pack.items] == ["backend_plan"]
    assert any(
        item.candidate_id == "frontend_plan" and item.outcome == "superseded"
        for item in result.trace.decisions
    )


async def test_visibility_permission_freshness_and_redaction_run_before_ranking() -> None:
    candidates = [
        _candidate(
            "router_only",
            "system",
            "router",
            purpose="agent_execution",
            visibility=["router"],
        ),
        _candidate(
            "wrong_agent",
            "memory",
            "private",
            consumers=["agent:*"],
            visibility=["agent"],
            allowed_agent_ids=["other"],
            purpose="agent_execution",
        ),
        _candidate(
            "denied",
            "knowledge",
            "denied",
            purpose="agent_execution",
            permission_granted=False,
        ),
        _candidate(
            "expired",
            "memory",
            "expired",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            purpose="agent_execution",
        ),
        _candidate(
            "safe",
            "frontend_context",
            "safe",
            authority="host_asserted",
            structured_value={"page": "inbox", "token": "secret"},
            metadata={"api_key": "secret"},
            purpose="agent_execution",
        ),
    ]
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    result = await pipeline.assemble_candidates(
        request=_request(),
        purpose="agent_execution",
        consumer="agent:summarizer",
        candidates=candidates,
        budget=ContextBudget(max_tokens=500),
    )

    assert [item.item_id for item in result.pack.items] == ["safe"]
    assert "secret" not in str(result.pack.model_dump(mode="json"))
    outcomes = {item.candidate_id: item.outcome for item in result.trace.decisions}
    assert outcomes["router_only"] == "visibility_denied"
    assert outcomes["wrong_agent"] == "visibility_denied"
    assert outcomes["denied"] == "permission_denied"
    assert outcomes["expired"] == "expired"


async def test_frontend_provider_content_does_not_reintroduce_secret_fields() -> None:
    from app.services.context_providers import FrontendContextProvider

    request = _request().model_copy(
        update={
            "frontend_context": {
                "page": "inbox",
                "token": "frontend-secret",
                "raw": "raw-secret",
            }
        }
    )
    result = await ContextPipelineService(Settings(context_pipeline_mode="enforced")).assemble(
        request=request,
        purpose="route_decision",
        consumer="router",
        providers=[FrontendContextProvider()],
        budget=ContextBudget(max_tokens=500),
    )

    rendered = str(result.projection.payload)
    assert "inbox" in rendered
    assert "frontend-secret" not in rendered
    assert "raw-secret" not in rendered


def _candidate(
    candidate_id,
    source,
    content,
    *,
    authority="derived",
    consumers=None,
    visibility=None,
    purpose="route_decision",
    **kwargs,
):
    return ContextCandidate(
        candidate_id=candidate_id,
        source=source,
        scope="request",
        content=content,
        purpose=purpose,
        consumers=consumers or ["router", "agent:summarizer"],
        authority=authority,
        visibility=visibility or [],
        **kwargs,
    )


async def _assemble(candidates):
    return await ContextPipelineService(
        Settings(context_pipeline_mode="enforced")
    ).assemble_candidates(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        candidates=candidates,
        budget=ContextBudget(max_tokens=500),
    )
