from app.core.config import Settings
from app.schemas.context import ContextBudget, ContextCandidate
from app.schemas.routing import RouteRequest
from app.services.context_pipeline_service import ContextPipelineService


def _request() -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "request_conflict",
            "session_id": "session_conflict",
            "user": {"id": "user_conflict"},
            "input": {"text": "Use English this time"},
        }
    )


async def test_stable_reference_and_content_duplicates_do_not_consume_budget() -> None:
    result = await _assemble(
        [
            _candidate("result", "recent_result", "same output", source_ref="result:1"),
            _candidate("event", "recent_event", "same output", source_ref="result:1"),
            _candidate("evidence", "evidence", "same fact"),
            _candidate("knowledge", "knowledge", "same fact"),
        ]
    )

    assert len(result.pack.items) == 2
    assert (
        len([item for item in result.trace.decisions if item.outcome == "duplicate_dropped"]) == 2
    )


async def test_current_input_overrides_memory_for_turn_without_mutating_memory() -> None:
    memory = _candidate(
        "memory_preference",
        "memory",
        "Chinese",
        conflict_key="preference:language",
        fact_value="Chinese",
    )
    result = await _assemble(
        [
            _candidate(
                "current_input",
                "current_input",
                "English",
                authority="authoritative",
                conflict_key="preference:language",
                fact_value="English",
                must_include=True,
            ),
            memory,
        ]
    )

    assert [item.item_id for item in result.pack.items] == ["current_input"]
    assert memory.content == "Chinese"
    assert any(item.outcome == "overridden_for_turn" for item in result.trace.decisions)


async def test_high_risk_authoritative_conflict_becomes_bounded_router_signal() -> None:
    result = await _assemble(
        [
            _candidate(
                "state_a",
                "current_plan",
                "approved",
                authority="authoritative",
                conflict_key="payment:approval",
                fact_value="approved",
                metadata={"risk": "high"},
            ),
            _candidate(
                "state_b",
                "recent_event",
                "rejected",
                authority="authoritative",
                conflict_key="payment:approval",
                fact_value="rejected",
                metadata={"risk": "high"},
            ),
        ]
    )

    assert [item.source for item in result.pack.items] == ["system"]
    assert "requires clarification" in result.pack.items[0].content
    assert (
        len([item for item in result.trace.decisions if item.outcome == "unresolved_conflict"]) == 2
    )


def _candidate(candidate_id, source, content, **kwargs):
    return ContextCandidate(
        candidate_id=candidate_id,
        source=source,
        scope="request",
        content=content,
        purpose="route_decision",
        consumers=["router"],
        authority=kwargs.pop("authority", "derived"),
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
