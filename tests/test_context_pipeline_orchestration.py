import asyncio

from app.core.config import Settings
from app.schemas.context import ContextAssemblySession, ContextBudget, ContextCandidate
from app.schemas.routing import RouteRequest
from app.services.context_pipeline_service import ContextPipelineService
from app.services.context_providers import (
    ArtifactProvider,
    BaseContextProvider,
    CurrentAgentProvider,
    CurrentInputProvider,
    EvidenceContextProvider,
    FrontendContextProvider,
    HistoryProvider,
    ResultProvider,
)


def _request() -> RouteRequest:
    return RouteRequest.model_validate(
        {
            "request_id": "request_pipeline",
            "session_id": "session_pipeline",
            "user": {"id": "user_pipeline", "attributes": {"tenant_id": "tenant_pipeline"}},
            "input": {"text": "route this"},
            "current_agent": {"agent_id": "summarizer"},
            "frontend_context": {"page": "inbox"},
        }
    )


async def test_pipeline_collects_provider_candidates_and_keeps_trace_outside_projection() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    result = await pipeline.assemble(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        providers=[CurrentInputProvider(), CurrentAgentProvider(), FrontendContextProvider()],
        budget=ContextBudget(max_tokens=500),
    )

    sources = {item.source for item in result.pack.items}
    assert sources == {"current_input", "current_agent", "frontend_context"}
    frontend = next(item for item in result.pack.items if item.source == "frontend_context")
    assert frontend.authority == "host_asserted"
    assert "provider_outcomes" not in str(result.projection.payload)
    assert [item.status for item in result.trace.provider_outcomes] == ["ok", "ok", "ok"]


async def test_pipeline_records_skip_timeout_error_and_does_not_fail_optional_provider() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    result = await pipeline.assemble(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        providers=[SkippedProvider(), SlowProvider(), ErrorProvider(), StaticProvider()],
        budget=ContextBudget(max_tokens=500),
    )

    statuses = {item.provider: item.status for item in result.trace.provider_outcomes}
    assert statuses == {
        "skipped": "skipped",
        "slow": "timeout",
        "error": "error",
        "static": "ok",
    }
    assert [item.source for item in result.pack.items] == ["system"]


async def test_request_scoped_provider_cache_is_reused_only_in_same_assembly() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    provider = CountingProvider()
    session = ContextAssemblySession(
        assembly_id="assembly_1",
        request_id="request_pipeline",
        user_id="user_pipeline",
        tenant_id="tenant_pipeline",
    )
    kwargs = {
        "request": _request(),
        "purpose": "route_decision",
        "consumer": "router",
        "providers": [provider],
        "budget": ContextBudget(max_tokens=500),
        "assembly_session": session,
    }

    first = await pipeline.assemble(**kwargs)
    second = await pipeline.assemble(**kwargs)
    third = await pipeline.assemble(
        **{**kwargs, "assembly_session": None},
    )
    other_user_request = _request().model_copy(
        update={"user": _request().user.model_copy(update={"id": "other_user"})}
    )
    await pipeline.assemble(
        **{
            **kwargs,
            "request": other_user_request,
        },
    )

    assert provider.calls == 3
    assert first.trace.provider_outcomes[0].cache_hit is False
    assert second.trace.provider_outcomes[0].cache_hit is True
    assert third.trace.provider_outcomes[0].cache_hit is False


async def test_history_result_evidence_and_artifact_providers_return_bounded_candidates() -> None:
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))
    result = await pipeline.assemble(
        request=_request(),
        purpose="route_decision",
        consumer="router",
        providers=[
            HistoryProvider(),
            ResultProvider(),
            EvidenceContextProvider(),
            ArtifactProvider(),
        ],
        sources={
            "host_history": [{"message_id": "message_1", "role": "user", "content": "earlier"}],
            "recent_results": [
                {
                    "result_id": "result_1",
                    "run_id": "run_1",
                    "agent_id": "summarizer",
                    "status": "completed",
                    "output": {"summary": "bounded result", "raw": "x" * 5000},
                    "artifact_refs": ["memory://artifact_1"],
                }
            ],
            "evidence": [{"id": "evidence_1", "content": "bounded evidence", "score": 0.9}],
        },
        budget=ContextBudget(max_tokens=1000),
    )

    assert {item.source for item in result.pack.items} == {
        "host_history",
        "recent_result",
        "evidence",
        "artifact",
    }
    assert "x" * 100 not in str(result.projection.payload)


async def test_provider_cannot_mutate_original_request_or_consumer_outputs() -> None:
    request = _request()
    pipeline = ContextPipelineService(Settings(context_pipeline_mode="enforced"))

    result = await pipeline.assemble(
        request=request,
        purpose="route_decision",
        consumer="router",
        providers=[MutatingProvider()],
        budget=ContextBudget(max_tokens=500),
    )

    assert request.frontend_context == {"page": "inbox"}
    assert "mutated" not in str(result.projection.payload)
    assert "mutated" not in str(result.pack.metadata)


class StaticProvider(BaseContextProvider):
    name = "static"

    async def collect(self, context):
        return [
            ContextCandidate(
                candidate_id="system_fact",
                source="system",
                scope="request",
                content="bounded fact",
                purpose=context.purpose,
                consumers=[context.consumer],
                authority="authoritative",
            )
        ]


class SkippedProvider(StaticProvider):
    name = "skipped"

    def applies(self, context):
        return False


class SlowProvider(StaticProvider):
    name = "slow"
    timeout_seconds = 0.01

    async def collect(self, context):
        await asyncio.sleep(0.05)
        return await super().collect(context)


class ErrorProvider(StaticProvider):
    name = "error"

    async def collect(self, context):
        raise RuntimeError("provider failed")


class CountingProvider(StaticProvider):
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    async def collect(self, context):
        self.calls += 1
        return await super().collect(context)


class MutatingProvider(StaticProvider):
    name = "mutating"

    async def collect(self, context):
        context.request.frontend_context["mutated"] = True
        return []
