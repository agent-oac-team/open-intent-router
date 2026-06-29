from uuid import uuid4

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import RoutingError
from app.llm.client import LLMClient
from app.llm.mock import MockLLMClient
from app.llm.openai_compatible import OpenAICompatibleLLMClient
from app.schemas.agents import AgentDefinition
from app.schemas.routing import (
    InvocationPreview,
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.services.context_service import ContextService
from app.services.invocation_service import build_invocation_input, missing_required_inputs
from app.services.plan_builder import build_ordered_plan_from_text
from app.services.plan_service import PlanService
from app.services.registry_service import AgentRegistryService


class RouterService:
    def __init__(
        self,
        settings: Settings,
        registry: AgentRegistryService,
        llm_client: LLMClient | None = None,
        context_service: ContextService | None = None,
        chat_history_service=None,
        result_repository=None,
        route_log_repository=None,
        evidence_provider=None,
        plan_service: PlanService | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.llm_client = llm_client or _llm_client(settings)
        self.context_service = context_service or ContextService(settings)
        self.chat_history_service = chat_history_service
        self.result_repository = result_repository
        self.route_log_repository = route_log_repository
        self.evidence_provider = evidence_provider
        self.plan_service = plan_service

    async def route(self, request: RouteRequest) -> RouteResponse:
        request_id = request.request_id or f"req_{uuid4().hex}"
        candidates = await self.registry.candidates_for_user(request.user)
        candidate_ids = [agent.agent_id for agent in candidates]
        host_history = await self._host_history(request)
        agent_history = await self._agent_history(request)
        recent_results = await self._recent_results(request)
        evidence_result = await self._evidence(request, candidate_ids)
        if evidence_result.route_override:
            return await self._route_from_evidence_override(
                request,
                request_id,
                candidate_ids,
                evidence_result,
            )
        base_context = self.context_service.build_route_context(
            request,
            candidate_agent_ids=candidate_ids,
            host_history=host_history,
            agent_history=agent_history,
            recent_results=recent_results,
            evidence=evidence_result.evidence,
            intent_hint=evidence_result.intent_hint,
        )
        if not candidates:
            response = RouteResponse(
                request_id=request_id,
                session_id=request.session_id,
                decision=RouteDecision(
                    status="unsupported",
                    action="unsupported",
                    confidence=0,
                    reason="No candidate Agent is available.",
                    message="No available Agent can handle this request.",
                ),
                context=base_context,
            )
            await self._after_route(request, response)
            return response

        output = await self.llm_client.route(
            LLMRouteInput(request=request, candidates=candidates, context=base_context)
        )
        output = self._ensure_plan_for_multi_task(output, request, candidates)
        output = await self._apply_plan_policy(output)
        output = output.model_copy(update={"request_id": request_id})
        output = await self._post_validate(output, request)
        response = await self._clarify_or_attach_invocation(output, request)
        await self._after_route(request, response)
        return response

    async def _post_validate(self, output: RouteResponse, request: RouteRequest) -> RouteResponse:
        candidate_ids = set(output.context.candidate_agent_ids)
        if output.decision.target_agent_id and output.decision.target_agent_id not in candidate_ids:
            raise RoutingError("Router selected an Agent outside the candidate set")
        if output.decision.action == "continue_agent":
            current = request.current_agent.agent_id if request.current_agent else None
            if output.decision.target_agent_id != current:
                raise RoutingError("continue_agent target must match current Agent")
        try:
            return RouteResponse.model_validate(output.model_dump())
        except ValidationError as exc:
            raise RoutingError("Router output validation failed", details={"errors": exc.errors()}) from exc

    async def _clarify_or_attach_invocation(
        self,
        output: RouteResponse,
        request: RouteRequest,
    ) -> RouteResponse:
        target = output.decision.target_agent_id
        if output.decision.action not in {"open_agent", "continue_agent"} or not target:
            return output
        agent = await self.registry.get_definition(target)
        if agent is None:
            raise RoutingError("Selected Agent no longer exists")
        invocation_input = _build_invocation_input(agent, output, request)
        missing = _missing_required_inputs(agent, invocation_input)
        if missing:
            return output.model_copy(
                update={
                    "decision": RouteDecision(
                        status="clarify",
                        action="clarify",
                        confidence=output.decision.confidence,
                        reason=f"Missing required inputs: {', '.join(missing)}",
                        message=f"Please provide: {', '.join(missing)}.",
                    ),
                    "invocation": None,
                }
            )
        return output.model_copy(
            update={
                "invocation": InvocationPreview(
                    mode="deferred",
                    agent_id=agent.agent_id,
                    input=invocation_input,
                )
            }
        )

    async def _host_history(self, request: RouteRequest) -> list[dict]:
        if not self.chat_history_service:
            return []
        return [
            item.model_dump()
            for item in await self.chat_history_service.get_host_history(request.session_id)
        ]

    async def _agent_history(self, request: RouteRequest) -> list[dict]:
        if not self.chat_history_service or not request.current_agent:
            return []
        return [
            item.model_dump()
            for item in await self.chat_history_service.get_agent_history(
                request.session_id,
                request.current_agent.agent_id,
            )
        ]

    async def _recent_results(self, request: RouteRequest) -> list[dict]:
        if not self.result_repository:
            return []
        return [
            item.model_dump()
            for item in await self.result_repository.list_recent(
                request.session_id,
                limit=self.settings.router_max_recent_results,
            )
        ]

    async def _evidence(self, request: RouteRequest, candidate_agent_ids: list[str]):
        if not self.evidence_provider:
            from app.plugins.evidence import EvidenceResult

            return EvidenceResult()
        return await self.evidence_provider.match(
            question=request.input.text,
            candidate_agent_ids=candidate_agent_ids,
            user=request.user,
        )

    async def _route_from_evidence_override(
        self,
        request: RouteRequest,
        request_id: str,
        candidate_agent_ids: list[str],
        evidence_result,
    ) -> RouteResponse:
        override = evidence_result.route_override or {}
        target_agent_id = override.get("target_agent_id")
        if target_agent_id and target_agent_id not in candidate_agent_ids:
            raise RoutingError("Evidence route_override target is not available to the user")
        response = RouteResponse(
            request_id=request_id,
            session_id=request.session_id,
            decision=RouteDecision(
                status="ok",
                action=override.get("action", "open_agent"),
                target_agent_id=target_agent_id,
                confidence=1.0,
                reason="Matched fixed-question route override.",
                message=override.get("message", "Matched a fixed question."),
            ),
            context=RouteContext(
                relation="new_task",
                current_agent_id=request.current_agent.agent_id if request.current_agent else None,
                candidate_agent_ids=candidate_agent_ids,
                intent_hint=evidence_result.intent_hint,
                evidence=evidence_result.evidence,
            ),
        )
        response = await self._clarify_or_attach_invocation(response, request)
        await self._after_route(request, response)
        return response

    async def _after_route(self, request: RouteRequest, response: RouteResponse) -> None:
        if self.plan_service and response.plan is not None:
            await self.plan_service.save_plan(response.plan)
        if self.chat_history_service:
            await self.chat_history_service.record_user_input(
                session_id=request.session_id,
                user_id=request.user.id,
                content=request.input.text,
                source=request.source,
                request_id=response.request_id,
                event_id=request.event_id,
                agent_id=request.current_agent.agent_id if request.current_agent else None,
                agent_session_id=(
                    request.current_agent.agent_session_id if request.current_agent else None
                ),
            )
        if self.route_log_repository:
            from app.schemas.logs import RouteLog

            await self.route_log_repository.add(
                RouteLog(
                    request_id=response.request_id,
                    session_id=response.session_id,
                    model_name=self.settings.router_llm_model,
                    candidate_agent_ids=response.context.candidate_agent_ids,
                    prompt_summary=request.input.text[:500],
                    evidence=response.context.evidence,
                    parsed_output={
                        **response.model_dump(),
                        "execution_policy": response.execution_policy,
                        "next_action": (
                            response.next_action.model_dump(mode="json")
                            if response.next_action
                            else None
                        ),
                    },
                    validation_status="ok",
                )
            )

    def _ensure_plan_for_multi_task(
        self,
        output: RouteResponse,
        request: RouteRequest,
        candidates,
    ) -> RouteResponse:
        if output.plan is not None or output.context.relation != "multi_task":
            return output
        plan = build_ordered_plan_from_text(
            text=request.input.text,
            session_id=request.session_id,
            candidates=candidates,
        )
        if plan is None:
            return output
        return output.model_copy(
            update={
                "decision": RouteDecision(
                    status=output.decision.status,
                    action="show_plan",
                    target_agent_id=None,
                    confidence=output.decision.confidence,
                    reason=output.decision.reason or "Detected an ordered multi-agent task.",
                    message=output.decision.message or "已生成多步骤执行计划。",
                ),
                "context": output.context.model_copy(update={"relation": "multi_task"}),
                "plan": plan,
                "invocation": None,
            }
        )

    async def _apply_plan_policy(self, output: RouteResponse) -> RouteResponse:
        if output.plan is None:
            return output
        policy = (
            output.execution_policy
            or output.plan.execution_policy
            or await self._metadata_policy_for_plan(output.plan)
        )
        policy = policy or self.settings.default_plan_execution_policy
        if self.settings.app_env != "local" and policy == "auto_execute":
            policy = "require_confirmation"
        if (
            self.settings.app_env == "local"
            and policy == "auto_execute"
            and not self.settings.allow_local_auto_execute_plans
        ):
            policy = "require_confirmation"

        next_action = output.next_action or output.plan.next_action
        if next_action is None:
            from app.schemas.plans import NextAction

            if policy == "require_confirmation":
                next_action = NextAction(
                    type="confirm_plan",
                    message="请确认是否执行该计划。",
                    plan_id=output.plan.plan_id,
                )
            elif policy == "host_managed":
                next_action = NextAction(
                    type="wait_for_agent_event",
                    message="该计划由宿主应用继续执行。",
                    plan_id=output.plan.plan_id,
                )

        plan = output.plan.model_copy(
            update={"execution_policy": policy, "next_action": next_action}
        )
        return output.model_copy(
            update={"execution_policy": policy, "next_action": next_action, "plan": plan}
        )

    async def _metadata_policy_for_plan(self, plan) -> str | None:
        for step in plan.steps:
            definition = await self.registry.get_definition(step.agent_id)
            if not definition:
                continue
            execution = definition.metadata.get("execution")
            if isinstance(execution, dict) and execution.get("policy"):
                return str(execution["policy"])
            if definition.metadata.get("execution_policy"):
                return str(definition.metadata["execution_policy"])
        return None


def _missing_required_inputs(agent: AgentDefinition, invocation_input: dict) -> list[str]:
    return missing_required_inputs(agent, invocation_input)


def _build_invocation_input(
    agent: AgentDefinition,
    output: RouteResponse,
    request: RouteRequest,
) -> dict:
    return build_invocation_input(agent, request.input.text)


def _llm_client(settings: Settings) -> LLMClient:
    if settings.router_llm_provider == "mock":
        return MockLLMClient()
    return OpenAICompatibleLLMClient(settings)
