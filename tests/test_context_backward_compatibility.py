from app.plugins.evidence import EvidenceResult
from app.schemas.agent_context import KnowledgeContext, MemoryContext
from app.schemas.common import ErrorDetail
from app.schemas.plans import Plan
from app.schemas.routing import (
    InvocationPreview,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.services.router_service import RouterService


def test_route_response_public_context_and_invocation_contract_remains_stable() -> None:
    response = RouteResponse(
        request_id="request_1",
        session_id="session_1",
        assistant_message="Visible answer",
        decision=RouteDecision(
            action="open_agent",
            target_agent_id="summarizer",
            message="Compatibility answer",
        ),
        context=RouteContext(
            candidate_agent_ids=["summarizer"],
            metadata={"context_pack": {"pack_id": "pack_1", "selection": []}},
        ),
        invocation=InvocationPreview(
            agent_id="summarizer",
            input={
                "text": "hello",
                "memory_context": MemoryContext(status="empty").model_dump(mode="json"),
                "knowledge_context": KnowledgeContext(status="disabled").model_dump(mode="json"),
            },
        ),
    )

    payload = response.model_dump(mode="json")

    assert payload["assistant_message"] == "Visible answer"
    assert payload["decision"]["message"] == "Compatibility answer"
    assert payload["invocation"]["input"]["text"] == "hello"
    assert payload["invocation"]["input"]["memory_context"]["status"] == "empty"
    assert payload["invocation"]["input"]["knowledge_context"]["status"] == "disabled"
    assert payload["context"]["metadata"]["context_pack"]["pack_id"] == "pack_1"


def test_plan_and_error_public_contracts_remain_parseable() -> None:
    plan = Plan.model_validate(
        {
            "plan_id": "plan_1",
            "user_id": "u1",
            "tenant_id": "t1",
            "session_id": "session_1",
            "status": "pending",
            "steps": [
                {
                    "step_id": "step_1",
                    "agent_id": "summarizer",
                    "description": "summarize",
                }
            ],
        }
    )
    response = RouteResponse(
        request_id="request_plan",
        session_id="session_1",
        decision=RouteDecision(action="show_plan", message="Plan ready"),
        context=RouteContext(candidate_agent_ids=["summarizer"]),
        plan=plan,
        error=ErrorDetail(
            code="compatibility_error",
            message="bounded error",
            details={"field": "input"},
        ),
    )

    payload = response.model_dump(mode="json")

    assert payload["plan"]["plan_id"] == "plan_1"
    assert payload["plan"]["steps"][0]["step_id"] == "step_1"
    assert payload["error"] == {
        "code": "compatibility_error",
        "message": "bounded error",
        "details": {"field": "input"},
    }


async def test_strong_evidence_override_keeps_existing_route_contract(
    settings,
    registry_service,
) -> None:
    service = RouterService(
        settings=settings.model_copy(update={"context_pipeline_mode": "enforced"}),
        registry=registry_service,
        evidence_provider=StrongEvidenceProvider(),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "session_evidence",
                "user": {"id": "user_1", "roles": ["operator"]},
                "input": {"text": "fixed question"},
            }
        )
    )

    assert response.decision.action == "open_agent"
    assert response.decision.target_agent_id == "summarizer"
    assert response.assistant_message == "Open summarizer"
    assert response.invocation and response.invocation.input["text"] == "fixed question"


class StrongEvidenceProvider:
    async def match(self, *, question, candidate_agent_ids, user):
        return EvidenceResult(
            route_override={
                "action": "open_agent",
                "target_agent_id": "summarizer",
                "message": "Open summarizer",
            },
            evidence=[
                {
                    "id": "fixed_question",
                    "type": "fixed_question",
                    "content": "fixed question",
                    "score": 1.0,
                }
            ],
        )
