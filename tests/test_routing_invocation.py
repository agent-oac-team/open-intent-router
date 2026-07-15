import pytest

from app.core.errors import RoutingError
from app.schemas.events import AgentEvent
from app.schemas.invocation import InvokeRequest
from app.schemas.routing import (
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.services.invocation_service import InvocationService, build_default_invoker_registry
from app.services.plan_service import PlanService
from app.services.router_service import RouterService


async def test_mock_router_returns_invocation_preview(settings, registry_service) -> None:
    service = RouterService(settings=settings, registry=registry_service)
    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize this text"},
            }
        )
    )
    assert response.decision.action == "open_agent"
    assert response.assistant_message == "Routing to Summarizer."
    assert response.invocation
    assert response.invocation.input["text"] == "summarize this text"


async def test_router_collapses_single_step_plan_to_agent_route(settings, registry_service) -> None:
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=SingleStepPlanLLM(),
    )
    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize this text"},
            }
        )
    )

    assert response.decision.action == "open_agent"
    assert response.decision.target_agent_id == "summarizer"
    assert response.context.relation == "new_task"
    assert response.plan is None
    assert response.next_action is None
    assert response.invocation
    assert response.invocation.agent_id == "summarizer"


async def test_mock_router_creates_and_persists_multi_agent_plan(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    plan_service = PlanService(repositories["plans"])
    service = RouterService(
        settings=settings,
        registry=registry_service,
        plan_service=plan_service,
    )
    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {
                    "id": "u1",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "t1"},
                },
                "input": {"text": "first summarize this text, then create a task"},
            }
        )
    )

    assert response.decision.action == "reply"
    assert response.context.relation == "multi_task"
    assert response.execution_policy == "require_confirmation"
    assert response.next_action
    assert response.next_action.type == "confirm_plan"
    assert response.plan
    assert response.plan.execution_policy == "require_confirmation"
    assert [step.agent_id for step in response.plan.steps] == ["summarizer", "task_creator"]
    assert response.plan.steps[1].depends_on == [response.plan.steps[0].step_id]
    assert await repositories["plans"].get(response.plan.plan_id, tenant_id="t1", user_id="u1")


async def test_router_overwrites_forged_plan_owner_requires_tenant_and_preserves_event_owner(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    plan_service = PlanService(repositories["plans"])
    service = RouterService(
        settings=settings,
        registry=registry_service,
        plan_service=plan_service,
        llm_client=ForgedMultiStepPlanLLM(),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "owned_session",
                "user": {
                    "id": "trusted_user",
                    "roles": ["operator"],
                    "attributes": {"tenant_id": "trusted_tenant"},
                },
                "input": {"text": "summarize this text and create a task"},
            }
        )
    )

    assert response.plan is not None
    assert (response.plan.tenant_id, response.plan.user_id) == (
        "trusted_tenant",
        "trusted_user",
    )
    assert (
        await repositories["plans"].get(
            response.plan.plan_id,
            tenant_id="forged_tenant",
            user_id="forged_user",
        )
        is None
    )
    progressed = await plan_service.apply_agent_event(
        AgentEvent(
            event_id="owned_progress",
            session_id="owned_session",
            agent_id="summarizer",
            plan_id=response.plan.plan_id,
            step_id="step_1",
            event_type="agent_progress",
        ),
        tenant_id="trusted_tenant",
        user_id="trusted_user",
    )
    assert progressed is not None
    assert (progressed.tenant_id, progressed.user_id) == ("trusted_tenant", "trusted_user")

    with pytest.raises(RoutingError, match="Trusted tenant identity is required"):
        await service.route(
            RouteRequest.model_validate(
                {
                    "session_id": "missing_tenant_session",
                    "user": {"id": "trusted_user", "roles": ["operator"]},
                    "input": {"text": "summarize this text and create a task"},
                }
            )
        )


async def test_router_generates_assistant_message_for_legacy_llm_output(
    settings, registry_service
) -> None:
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=LegacyOpenAgentLLM(),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize this text"},
            }
        )
    )

    assert response.assistant_message == "Routing to Summarizer."
    assert response.decision.message == "Routing to Summarizer."
    assert response.invocation


async def test_router_observes_tag_matches_without_filtering_candidates(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    llm = CapturingLLM(target_agent_id="task_creator")
    evidence = CapturingEvidenceProvider()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=llm,
        evidence_provider=evidence,
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "please create a task"},
            }
        )
    )

    assert evidence.candidate_agent_ids == ["summarizer", "task_creator"]
    assert llm.candidate_agent_ids == ["summarizer", "task_creator"]
    assert response.context.candidate_agent_ids == ["summarizer", "task_creator"]
    assert response.context.metadata["tag_filter"] == "matched_but_not_applied"
    assert response.context.metadata["tag_filter_applied"] is False
    assert response.context.metadata["tag_filter_matched_agent_ids"] == ["task_creator"]
    assert response.context.metadata["filtered_candidate_agent_ids"] == [
        "summarizer",
        "task_creator",
    ]
    assert response.invocation
    assert response.invocation.agent_id == "task_creator"


async def test_router_falls_back_to_all_available_agents_when_tags_do_not_match(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    llm = CapturingLLM()
    service = RouterService(settings=settings, registry=registry_service, llm_client=llm)

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "please help with something unusual"},
            }
        )
    )

    assert llm.candidate_agent_ids == ["summarizer", "task_creator"]
    assert response.context.metadata["tag_filter"] == "no_match_no_filter"
    assert response.context.metadata["tag_filter_applied"] is False
    assert response.context.metadata["filtered_candidate_agent_ids"] == [
        "summarizer",
        "task_creator",
    ]


async def test_router_returns_unsupported_when_no_agents_are_available(
    settings,
    registry_service,
) -> None:
    llm = CapturingLLM()
    service = RouterService(settings=settings, registry=registry_service, llm_client=llm)

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["guest"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize this text"},
            }
        )
    )

    assert response.decision.action == "unsupported"
    assert response.context.candidate_agent_ids == []
    assert response.context.metadata["tag_filter"] == "no_available_agents"
    assert llm.candidate_agent_ids is None


async def test_router_does_not_expand_candidates_beyond_access_policy(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    restricted_payload = task_creator_agent.model_dump(mode="json")
    restricted_payload["access_policy"] = {"allow_roles": ["admin"], "allow_tenants": ["*"]}
    restricted = task_creator_agent.__class__.model_validate(restricted_payload)
    await repositories["registry"].upsert(restricted)
    await registry_service.load()
    llm = CapturingLLM()
    service = RouterService(settings=settings, registry=registry_service, llm_client=llm)

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "please create a task"},
            }
        )
    )

    assert llm.candidate_agent_ids == ["summarizer"]
    assert response.context.candidate_agent_ids == ["summarizer"]
    assert "task_creator" not in response.context.metadata["available_agent_ids"]


async def test_router_strong_fixed_question_override_skips_llm(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    llm = FailingLLM()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=llm,
        evidence_provider=FixedQuestionOverrideEvidenceProvider("task_creator"),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "fixed task"},
            }
        )
    )

    assert llm.calls == 0
    assert response.decision.action == "open_agent"
    assert response.decision.target_agent_id == "task_creator"
    assert response.context.candidate_agent_ids == ["summarizer", "task_creator"]
    assert response.context.metadata["evidence_candidate_agent_ids"] == ["task_creator"]
    assert response.invocation
    assert response.invocation.agent_id == "task_creator"


async def test_router_strong_fixed_question_beats_tag_mismatch_when_allowed(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    await repositories["registry"].upsert(task_creator_agent)
    await registry_service.load()
    llm = FailingLLM()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=llm,
        evidence_provider=FixedQuestionOverrideEvidenceProvider("task_creator"),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "fixed but tagless question"},
            }
        )
    )

    assert llm.calls == 0
    assert response.decision.target_agent_id == "task_creator"
    assert response.context.metadata["tag_filter"] == "no_match_no_filter"
    assert response.context.metadata["filtered_candidate_agent_ids"] == [
        "summarizer",
        "task_creator",
    ]


async def test_router_denies_strong_fixed_question_override_outside_access_policy(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    restricted_payload = task_creator_agent.model_dump(mode="json")
    restricted_payload["access_policy"] = {"allow_roles": ["admin"], "allow_tenants": ["*"]}
    restricted = task_creator_agent.__class__.model_validate(restricted_payload)
    await repositories["registry"].upsert(restricted)
    await registry_service.load()
    llm = FailingLLM()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=llm,
        evidence_provider=FixedQuestionDeniedEvidenceProvider("task_creator"),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "fixed task"},
            }
        )
    )

    assert llm.calls == 0
    assert response.decision.status == "unsupported"
    assert response.decision.action == "unsupported"
    assert response.context.relation == "unsupported"
    assert response.context.candidate_agent_ids == ["summarizer"]
    assert response.context.metadata["permission_denied"] is True
    assert response.context.metadata["route_override_denied"]["target_agent_id"] == "task_creator"
    assert "无权限" in response.assistant_message
    assert response.invocation is None


async def test_router_clarifies_low_confidence(settings, registry_service) -> None:
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=LowConfidenceLLM(),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize maybe"},
            }
        )
    )

    assert response.decision.action == "clarify"
    assert response.invocation is None
    assert response.context.metadata["low_confidence"]["confidence"] == 0.1
    assert (
        response.context.metadata["low_confidence"]["threshold"]
        == settings.router_low_confidence_threshold
    )
    assert response.assistant_message


async def test_router_clarifies_when_required_input_is_missing(
    settings,
    registry_service,
    repositories,
    task_creator_agent,
) -> None:
    needs_account_payload = task_creator_agent.model_dump(mode="json")
    needs_account_payload.update(
        {
            "agent_id": "account_lookup",
            "name": "Account Lookup",
            "description": "Look up an account",
            "capabilities": ["lookup_account"],
            "trigger": {"keywords": ["account"]},
            "required_inputs": ["account_id"],
            "input_schema": {
                "type": "object",
                "required": ["account_id"],
                "properties": {"account_id": {"type": "string"}},
            },
        }
    )
    needs_account = task_creator_agent.__class__.model_validate(needs_account_payload)
    await repositories["registry"].upsert(needs_account)
    await registry_service.load()
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=FixedTargetLLM("account_lookup"),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"]},
                "input": {"text": "look up account"},
            }
        )
    )

    assert response.decision.action == "clarify"
    assert response.invocation is None
    assert response.next_action
    assert response.next_action.type == "collect_input"
    assert response.next_action.metadata["missing_inputs"] == ["account_id"]
    assert "account_id" in response.assistant_message


async def test_router_invokes_when_required_input_is_present(settings, registry_service) -> None:
    service = RouterService(
        settings=settings,
        registry=registry_service,
        llm_client=FixedTargetLLM("summarizer"),
    )

    response = await service.route(
        RouteRequest.model_validate(
            {
                "session_id": "s1",
                "user": {"id": "u1", "roles": ["operator"], "attributes": {"tenant_id": "t1"}},
                "input": {"text": "summarize this text"},
            }
        )
    )

    assert response.decision.action == "open_agent"
    assert response.invocation
    assert response.invocation.input["text"] == "summarize this text"


async def test_mock_invocation_persists_run_and_result(
    settings, registry_service, repositories
) -> None:
    service = InvocationService(
        registry=registry_service,
        run_repository=repositories["runs"],
        result_repository=repositories["results"],
        invokers=build_default_invoker_registry(settings),
    )
    result = await service.invoke(
        InvokeRequest(
            session_id="s1",
            agent_id="summarizer",
            user={
                "id": "u1",
                "roles": ["operator"],
                "attributes": {"tenant_id": "t1"},
            },
            input={"text": "hello"},
        )
    )
    assert result.status == "completed"
    assert await repositories["runs"].get_run(result.run_id)
    assert (await repositories["results"].list_recent("s1", tenant_id="t1", user_id="u1"))[
        0
    ].run_id == result.run_id


class SingleStepPlanLLM:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request.request_id or "req_single_step_plan",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="show_plan",
                target_agent_id=None,
                confidence=0.7,
                reason="Model wrapped a single intent as a plan.",
                message="已生成执行计划。",
            ),
            context=RouteContext(
                relation="multi_task",
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates],
            ),
            plan={
                "plan_id": "plan_single_step",
                "user_id": "u1",
                "tenant_id": "t1",
                "session_id": payload.request.session_id,
                "steps": [
                    {
                        "step_id": "step_1",
                        "agent_id": "summarizer",
                        "description": "Summarize the text.",
                    }
                ],
            },
        )


class ForgedMultiStepPlanLLM:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request.request_id or "req_forged_plan",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="show_plan",
                confidence=0.99,
                reason="multi-step request",
                message="plan ready",
            ),
            context=RouteContext(
                relation="multi_task",
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates],
            ),
            plan={
                "plan_id": "plan_forged_owner",
                "user_id": "forged_user",
                "tenant_id": "forged_tenant",
                "session_id": payload.request.session_id,
                "steps": [
                    {
                        "step_id": "step_1",
                        "agent_id": "summarizer",
                        "description": "Summarize the text.",
                    },
                    {
                        "step_id": "step_2",
                        "agent_id": "task_creator",
                        "description": "Create a follow-up task.",
                        "depends_on": ["step_1"],
                    },
                ],
            },
        )


class LegacyOpenAgentLLM:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request.request_id or "req_legacy",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id="summarizer",
                confidence=0.7,
                reason="Matched summarization intent.",
                message="Routing to Summarizer.",
            ),
            context=RouteContext(
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates]
            ),
        )


class CapturingLLM:
    def __init__(self, target_agent_id: str | None = None) -> None:
        self.candidate_agent_ids = None
        self.target_agent_id = target_agent_id

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        self.candidate_agent_ids = [agent.agent_id for agent in payload.candidates]
        if self.target_agent_id in self.candidate_agent_ids:
            target = self.target_agent_id
        else:
            target = self.candidate_agent_ids[0] if self.candidate_agent_ids else None
        return RouteResponse(
            request_id=payload.request.request_id or "req_capture",
            session_id=payload.request.session_id,
            assistant_message="Captured route.",
            decision=RouteDecision(
                status="ok" if target else "unsupported",
                action="open_agent" if target else "unsupported",
                target_agent_id=target,
                confidence=0.8,
                reason="Captured candidates.",
                message="Captured route.",
            ),
            context=RouteContext(candidate_agent_ids=self.candidate_agent_ids),
        )


class CapturingEvidenceProvider:
    def __init__(self) -> None:
        self.candidate_agent_ids = None

    async def match(self, *, question, candidate_agent_ids, user):
        from app.plugins.evidence import EvidenceResult

        self.candidate_agent_ids = candidate_agent_ids
        return EvidenceResult()


class FixedQuestionOverrideEvidenceProvider:
    def __init__(self, target_agent_id: str) -> None:
        self.target_agent_id = target_agent_id
        self.candidate_agent_ids = None

    async def match(self, *, question, candidate_agent_ids, user):
        from app.plugins.evidence import EvidenceResult

        self.candidate_agent_ids = candidate_agent_ids
        return EvidenceResult(
            intent_hint="fixed_question",
            candidate_agent_ids=[self.target_agent_id],
            route_override={
                "action": "open_agent",
                "target_agent_id": self.target_agent_id,
                "message": "Matched fixed question.",
            },
            evidence=[
                {
                    "type": "fixed_question",
                    "strength": "strong",
                    "matched_agent_ids": [self.target_agent_id],
                }
            ],
        )


class FixedQuestionDeniedEvidenceProvider:
    def __init__(self, target_agent_id: str) -> None:
        self.target_agent_id = target_agent_id
        self.candidate_agent_ids = None

    async def match(self, *, question, candidate_agent_ids, user):
        from app.plugins.evidence import EvidenceResult

        self.candidate_agent_ids = candidate_agent_ids
        return EvidenceResult(
            intent_hint="fixed_question",
            route_override_denied={
                "action": "open_agent",
                "target_agent_id": self.target_agent_id,
                "reason": "permission_denied",
            },
            evidence=[
                {
                    "type": "fixed_question",
                    "strength": "strong",
                    "route_override_denied": True,
                    "route_override_target_agent_id": self.target_agent_id,
                }
            ],
        )


class FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        self.calls += 1
        raise AssertionError("LLM should not be called for this route")


class LowConfidenceLLM:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        target = payload.candidates[0].agent_id
        return RouteResponse(
            request_id=payload.request.request_id or "req_low_confidence",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id=target,
                confidence=0.1,
                reason="Low confidence match.",
                message="Routing with low confidence.",
            ),
            context=RouteContext(
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates]
            ),
        )


class FixedTargetLLM:
    def __init__(self, target_agent_id: str) -> None:
        self.target_agent_id = target_agent_id

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        return RouteResponse(
            request_id=payload.request.request_id or "req_fixed",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id=self.target_agent_id,
                confidence=0.8,
                reason="Fixed target for test.",
                message=f"Routing to {self.target_agent_id}.",
            ),
            context=RouteContext(
                candidate_agent_ids=[agent.agent_id for agent in payload.candidates]
            ),
        )
