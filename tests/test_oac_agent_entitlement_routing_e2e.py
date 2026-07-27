import pytest

from app.core.config import Settings
from app.core.errors import RoutingError
from app.plugins.evidence import EvidenceResult
from app.repositories.memory import MemoryAgentDefinitionRepository
from app.schemas.agents import (
    AccessPolicy,
    AgentDefinition,
    InvocationSpec,
    TriggerSpec,
    UiHandoffSpec,
)
from app.schemas.plans import Plan, PlanStep
from app.schemas.routing import RouteContext, RouteDecision, RouteRequest, RouteResponse
from app.services.registry_service import AgentRegistryService
from app.services.router_service import RouterService

OPS = "workspace.operations.access"
SALES = "workspace.sales_enablement.access"


def _agent(agent_id: str, entitlements: list[str]) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        name=agent_id,
        description=f"{agent_id} route",
        type="ui_handoff",
        trigger=TriggerSpec(keywords=[agent_id]),
        access_policy=AccessPolicy(allow_tenants=["oac"], any_entitlements=entitlements),
        invocation=InvocationSpec(type="ui_handoff"),
        ui_handoff=UiHandoffSpec(mode="route", route=f"/{agent_id}"),
    )


async def _registry(*agents: AgentDefinition) -> AgentRegistryService:
    settings = Settings(
        storage_backend="memory",
        registry_backend="database",
        registry_file_fallback_on_empty=False,
    )
    registry = AgentRegistryService(
        settings=settings,
        repository=MemoryAgentDefinitionRepository(list(agents)),
    )
    await registry.load()
    return registry


def _request(
    entitlement: str,
    text: str,
    *,
    current_agent: str | None = None,
    current_agent_session_id: str | None = "agent-session-1",
):
    payload = {
        "session_id": "session-1",
        "user": {
            "id": "42",
            "roles": ["operator"],
            "entitlements": [entitlement],
            "attributes": {"tenant_id": "oac"},
        },
        "input": {"text": text},
    }
    if current_agent:
        payload["source"] = "agent_chat"
        payload["current_agent"] = {
            "agent_id": current_agent,
            "agent_session_id": current_agent_session_id,
        }
    return RouteRequest.model_validate(payload)


class SelectFirstLLM:
    def __init__(self, action="open_agent") -> None:
        self.action = action
        self.candidates = None

    async def route(self, payload):
        self.candidates = [item.agent_id for item in payload.candidates]
        target = self.candidates[0]
        return RouteResponse(
            request_id=payload.request.request_id or "request-1",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                action=self.action,
                target_agent_id=target,
                confidence=1,
            ),
            context=RouteContext(
                relation="continue_current" if self.action == "continue_agent" else "new_task",
                current_agent_id=(target if self.action == "continue_agent" else None),
                candidate_agent_ids=self.candidates,
            ),
        )


class MismatchedContinuationLLM:
    async def route(self, payload):
        current = payload.request.current_agent.agent_id
        target = next(item.agent_id for item in payload.candidates if item.agent_id != current)
        return RouteResponse(
            request_id=payload.request.request_id or "request-switch",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                action="continue_agent",
                target_agent_id=target,
                confidence=1,
            ),
            context=RouteContext(
                relation="continue_current",
                current_agent_id=target,
                candidate_agent_ids=[item.agent_id for item in payload.candidates],
            ),
        )


class MustNotRun:
    async def route(self, _payload):
        raise AssertionError("LLM must not run without authorized candidates")


class MustNotMatchEvidence:
    async def match(self, **_kwargs):
        raise AssertionError("Evidence must not run without authorized candidates")


class UnauthorizedOverride:
    async def match(self, **_kwargs):
        return EvidenceResult(
            route_override={"action": "open_agent", "target_agent_id": "production_schedule"}
        )


class UnauthorizedPlanLLM:
    async def route(self, payload):
        candidate = payload.candidates[0].agent_id
        return RouteResponse(
            request_id=payload.request.request_id or "request-plan",
            session_id=payload.request.session_id,
            decision=RouteDecision(action="show_plan"),
            context=RouteContext(candidate_agent_ids=[candidate], relation="multi_task"),
            plan=Plan(
                plan_id="plan-1",
                tenant_id="forged",
                user_id="forged",
                steps=[
                    PlanStep(
                        step_id="step-1",
                        agent_id="production_schedule",
                        description="unauthorized",
                    )
                ],
            ),
        )


class UnauthorizedTargetLLM:
    async def route(self, payload):
        return RouteResponse(
            request_id=payload.request.request_id or "request-target",
            session_id=payload.request.session_id,
            decision=RouteDecision(action="open_agent", target_agent_id="production_schedule"),
            context=RouteContext(candidate_agent_ids=["production_schedule"]),
        )


class UnauthorizedContinuationLLM:
    async def route(self, payload):
        return RouteResponse(
            request_id=payload.request.request_id or "request-unauthorized-continuation",
            session_id=payload.request.session_id,
            decision=RouteDecision(
                action="continue_agent",
                target_agent_id="production_schedule",
            ),
            context=RouteContext(
                relation="continue_current",
                current_agent_id="production_schedule",
                candidate_agent_ids=["production_schedule"],
            ),
        )


class RestoredPlanService:
    def __init__(self) -> None:
        self.plan = Plan(
            plan_id="restored-plan",
            tenant_id="oac",
            user_id="42",
            session_id="session-1",
            steps=[
                PlanStep(
                    step_id="restored-step",
                    agent_id="production_schedule",
                    description="unauthorized restored step",
                )
            ],
        )

    async def get_active_plan(self, *_args, **_kwargs):
        return self.plan

    async def get_plan(self, *_args, **_kwargs):
        return self.plan


async def test_entitlement_matrix_filters_production_and_dual_version_agents() -> None:
    production = _agent("production_schedule", [OPS])
    strategy = _agent("strategy_analysis", [OPS, SALES])
    registry = await _registry(production, strategy)

    ops = await registry.available_for_user(_request(OPS, "route").user)
    sales = await registry.available_for_user(_request(SALES, "route").user)
    assert ops.available_agents == ["production_schedule", "strategy_analysis"]
    assert sales.available_agents == ["strategy_analysis"]

    llm = SelectFirstLLM()
    response = await RouterService(
        settings=registry.settings, registry=registry, llm_client=llm
    ).route(_request(OPS, "production_schedule"))
    assert response.decision.target_agent_id == "production_schedule"
    assert llm.candidates == ["production_schedule", "strategy_analysis"]


async def test_no_entitled_candidate_skips_trigger_evidence_and_llm() -> None:
    registry = await _registry(_agent("production_schedule", [OPS]))
    response = await RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=MustNotRun(),
        evidence_provider=MustNotMatchEvidence(),
    ).route(_request(SALES, "production_schedule"))
    assert response.decision.action == "unsupported"
    assert response.context.candidate_agent_ids == []


async def test_evidence_override_cannot_restore_unauthorized_agent() -> None:
    registry = await _registry(
        _agent("production_schedule", [OPS]),
        _agent("strategy_analysis", [OPS, SALES]),
    )
    response = await RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=MustNotRun(),
        evidence_provider=UnauthorizedOverride(),
    ).route(_request(SALES, "production_schedule"))
    assert response.decision.action == "unsupported"
    assert response.context.metadata["permission_denied"] is True


async def test_unauthorized_generated_plan_step_returns_permission_denied() -> None:
    registry = await _registry(_agent("strategy_analysis", [OPS, SALES]))
    service = RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=UnauthorizedPlanLLM(),
    )
    response = await service.route(_request(SALES, "make a plan"))

    assert response.decision.action == "unsupported"
    assert response.decision.status == "unsupported"
    assert response.plan is None
    assert response.invocation is None
    assert response.context.metadata["permission_denied"] is True


async def test_llm_target_and_restored_plan_cannot_escape_candidates() -> None:
    registry = await _registry(_agent("strategy_analysis", [OPS, SALES]))
    target_service = RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=UnauthorizedTargetLLM(),
    )
    with pytest.raises(RoutingError, match="outside the candidate set"):
        await target_service.route(_request(SALES, "choose target"))

    restored_service = RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=MustNotRun(),
        plan_service=RestoredPlanService(),
    )
    with pytest.raises(RoutingError, match="Plan contains an Agent outside"):
        await restored_service.route(_request(SALES, "continue plan"))


async def test_continue_agent_requires_current_agent_to_remain_entitled() -> None:
    registry = await _registry(_agent("production_schedule", [OPS]))
    response = await RouterService(
        settings=registry.settings, registry=registry, llm_client=MustNotRun()
    ).route(_request(SALES, "continue", current_agent="production_schedule"))
    assert response.decision.action == "unsupported"


async def test_mismatched_continue_agent_is_normalized_to_authorized_switch() -> None:
    registry = await _registry(
        _agent("strategy_analysis", [OPS]),
        _agent("production_schedule", [OPS]),
    )
    response = await RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=MismatchedContinuationLLM(),
    ).route(_request(OPS, "改为内容生产", current_agent="strategy_analysis"))

    assert response.decision.action == "open_agent"
    assert response.decision.target_agent_id == "production_schedule"
    assert response.context.relation == "switch_agent"
    assert response.context.current_agent_id is None


async def test_unavailable_continue_agent_is_normalized_to_unsupported() -> None:
    registry = await _registry(_agent("strategy_analysis", [SALES]))
    response = await RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=UnauthorizedContinuationLLM(),
    ).route(_request(SALES, "改为内容生产", current_agent="strategy_analysis"))

    assert response.decision.action == "unsupported"
    assert response.decision.status == "unsupported"
    assert response.decision.target_agent_id is None
    assert response.context.relation == "unsupported"
    assert response.context.metadata["permission_denied"] is True


async def test_continue_agent_without_agent_session_opens_a_new_agent_run() -> None:
    registry = await _registry(_agent("strategy_analysis", [OPS]))
    response = await RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=SelectFirstLLM(action="continue_agent"),
    ).route(
        _request(
            OPS,
            "继续完善刚才的方案",
            current_agent="strategy_analysis",
            current_agent_session_id=None,
        )
    )

    assert response.decision.action == "open_agent"
    assert response.decision.target_agent_id == "strategy_analysis"
    assert response.context.relation == "new_task"
    assert response.context.current_agent_id is None
    assert response.context.metadata["route_normalization"]["reason"] == "agent_session_missing"


@pytest.mark.parametrize(
    "text",
    ["内容生产", "我喜欢给客户的文案是温和的风格"],
)
async def test_reported_old_session_inputs_never_fail_on_a_stale_prior_agent(text: str) -> None:
    registry = await _registry(
        _agent("strategy_analysis", [OPS]),
        _agent("content_production", [OPS]),
    )
    response = await RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=MismatchedContinuationLLM(),
    ).route(_request(OPS, text, current_agent="strategy_analysis"))

    assert response.decision.action == "open_agent"
    assert response.decision.target_agent_id == "content_production"
    assert response.context.current_agent_id is None


async def test_reported_preference_input_is_valid_without_prior_agent_evidence() -> None:
    registry = await _registry(_agent("strategy_analysis", [OPS]))
    response = await RouterService(
        settings=registry.settings,
        registry=registry,
        llm_client=SelectFirstLLM(),
    ).route(_request(OPS, "我喜欢给客户的文案是温和的风格"))

    assert response.decision.action == "open_agent"
    assert response.decision.target_agent_id == "strategy_analysis"
