from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.context import (
    ContextAssemblySession,
    ContextBudget,
    ContextCandidate,
    ContextPack,
    ContextProjection,
    ContextTrace,
    ContextUsage,
    ProviderOutcome,
)


def test_context_candidate_requires_governed_purpose_consumer_and_authority() -> None:
    candidate = ContextCandidate(
        candidate_id="candidate_1",
        source="frontend_context",
        scope="request",
        content="page signal",
        purpose="route_decision",
        consumers=["router"],
        authority="host_asserted",
        visibility=["router"],
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert candidate.purpose == "route_decision"
    assert candidate.consumers == ["router"]
    assert candidate.authority == "host_asserted"

    with pytest.raises(ValidationError):
        ContextCandidate(
            candidate_id="bad",
            source="system",
            scope="request",
            purpose="unknown",
            consumers=[],
            authority="authoritative",
        )


def test_pack_projection_trace_and_provider_outcome_keep_separate_identifiers() -> None:
    budget = ContextBudget(max_tokens=100)
    pack = ContextPack(
        pack_id="pack_1",
        trace_id="trace_1",
        request_id="request_1",
        session_id="session_1",
        purpose="route_decision",
        consumer="router",
        budget=budget,
        usage=ContextUsage(budget_tokens=100),
        policy_version="policy-v1",
        budget_version="budget-v1",
        projection_version="projection-v1",
    )
    projection = ContextProjection(
        projection_id="projection_1",
        pack_id=pack.pack_id,
        request_id=pack.request_id,
        session_id=pack.session_id,
        purpose=pack.purpose,
        consumer=pack.consumer,
        projection_version=pack.projection_version,
        projection_hash="hash",
    )
    trace = ContextTrace(
        trace_id=pack.trace_id,
        pack_id=pack.pack_id,
        request_id=pack.request_id,
        session_id=pack.session_id,
        purpose=pack.purpose,
        consumer=pack.consumer,
        policy_version=pack.policy_version,
        budget_version=pack.budget_version,
        projection_version=pack.projection_version,
        provider_outcomes=[ProviderOutcome(provider="request", status="ok", candidate_count=1)],
        projection_hash=projection.projection_hash,
    )

    assert projection.pack_id == pack.pack_id
    assert trace.trace_id == pack.trace_id
    assert trace.provider_outcomes[0].status == "ok"


def test_context_assembly_session_cache_is_request_scoped() -> None:
    first = ContextAssemblySession(
        assembly_id="assembly_1",
        request_id="request_1",
        user_id="user_1",
        tenant_id="tenant_1",
    )
    second = ContextAssemblySession(
        assembly_id="assembly_2",
        request_id="request_2",
        user_id="user_1",
        tenant_id="tenant_1",
    )

    first.provider_cache["key"] = ProviderOutcome(provider="memory", status="ok")

    assert "key" in first.provider_cache
    assert second.provider_cache == {}
