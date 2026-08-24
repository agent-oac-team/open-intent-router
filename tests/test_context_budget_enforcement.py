import pytest

from app.core.config import Settings
from app.schemas.agents import CandidateAgentV2
from app.schemas.context import ContextBudget, ContextCandidate
from app.schemas.routing import RouteRequest
from app.services.context_pipeline_service import ContextBudgetExhausted, ContextPipelineService


def _request(text: str = "current input") -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "req_budget",
            "session_id": "session_budget",
            "user": {"id": "user_budget"},
            "input": {"text": text},
        }
    )


async def test_must_include_never_breaks_final_projection_hard_budget() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    candidate = ContextCandidate(
        candidate_id="critical",
        source="current_input",
        scope="request",
        content="x" * 500,
        purpose="route_decision",
        consumers=["router"],
        authority="authoritative",
        must_include=True,
        priority=100,
    )

    result = await pipeline.assemble_candidates(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        candidates=[candidate],
        budget=ContextBudget(max_tokens=80, per_item_char_limit=64),
    )

    assert result.projection.token_estimate <= result.pack.budget.max_tokens
    assert result.pack.usage.used_tokens <= result.pack.budget.max_tokens


async def test_structured_value_is_projected_and_counted_in_final_budget() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    candidate = ContextCandidate(
        candidate_id="result_1",
        source="recent_result",
        scope="session",
        content="short",
        structured_value={"result_id": "result_1", "output": {"secret": "z" * 4000}},
        purpose="route_decision",
        consumers=["router"],
        authority="authoritative",
        source_ref="result:result_1",
    )

    result = await pipeline.assemble_candidates(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        candidates=[candidate],
        budget=ContextBudget(max_tokens=100, per_item_char_limit=200),
    )

    rendered = str(result.projection.payload)
    assert "z" * 100 not in rendered
    assert result.projection.token_estimate <= 100


async def test_critical_minimum_representation_over_budget_is_controlled_failure() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    candidate = ContextCandidate(
        candidate_id="critical",
        source="current_input",
        scope="request",
        content="cannot fit",
        purpose="route_decision",
        consumers=["router"],
        authority="authoritative",
        must_include=True,
        priority=100,
    )

    with pytest.raises(ContextBudgetExhausted) as exc_info:
        await pipeline.assemble_candidates(
            request=_request(),
            purpose="route_decision",
            consumer="router",
            candidates=[candidate],
            budget=ContextBudget(max_tokens=1, allow_summary_placeholder=False),
        )

    assert exc_info.value.code == "context_budget_exhausted"


async def test_model_window_reserves_system_schema_rules_candidates_and_output() -> None:
    settings = Settings(
        context_pipeline_mode="enforced",
        context_model_window_tokens=500,
        context_system_reserve_tokens=80,
        context_schema_reserve_tokens=80,
        context_rules_reserve_tokens=40,
        context_output_reserve_tokens=100,
    )
    pipeline = ContextPipelineService(settings)
    result = await pipeline.assemble_candidates(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        candidates=[],
        candidate_agents=[
            CandidateAgentV2(
                agent_id="agent_1",
                name="Agent",
                description="description " * 20,
            )
        ],
        budget=ContextBudget(max_tokens=500),
    )

    assert result.pack.budget.max_tokens < 200
    assert result.projection.token_estimate <= result.pack.budget.max_tokens


async def test_source_and_total_budgets_share_one_projection_path() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    candidates = [
        ContextCandidate(
            candidate_id=source,
            source=source,
            scope="request",
            content=(source + " ") * 80,
            purpose="route_decision",
            consumers=["router"],
            authority="derived",
        )
        for source in (
            "evidence",
            "memory",
            "knowledge",
            "host_history",
            "recent_result",
            "frontend_context",
        )
    ]

    result = await pipeline.assemble_candidates(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        candidates=candidates,
        budget=ContextBudget(
            max_tokens=180,
            source_budgets={source: 20 for source in {item.source for item in candidates}},
            per_item_char_limit=200,
        ),
    )

    assert result.projection.token_estimate <= 180
    assert all(item.token_estimate <= 20 for item in result.pack.items)
    assert result.pack.usage.dropped_count > 0


def test_context_pipeline_has_no_llm_dependency_for_selection_or_summary() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))

    assert not hasattr(pipeline, "llm_client")
