import re
from copy import copy
from dataclasses import dataclass
from uuid import uuid4

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import RoutingError
from app.core.redaction import redact_value
from app.llm.client import LLMClient
from app.llm.mock import MockLLMClient
from app.llm.openai_compatible import OpenAICompatibleLLMClient
from app.schemas.agents import AgentDefinition
from app.schemas.common import JsonDict
from app.schemas.plans import NextAction
from app.schemas.routing import (
    InvocationPreview,
    LLMRouteInput,
    RouteContext,
    RouteDecision,
    RouteRequest,
    RouteResponse,
)
from app.schemas.turns import TurnUserInput
from app.services.context_service import ContextService
from app.services.invocation_service import build_invocation_input, missing_required_inputs
from app.services.plan_builder import build_ordered_plan_from_text
from app.services.plan_service import PlanService
from app.services.registry_service import AgentRegistryService
from app.services.task_continuation import requests_plan_continuation
from app.services.turn_service import TurnService


class RouterService:
    def __init__(
        self,
        settings: Settings,
        registry: AgentRegistryService,
        llm_client: LLMClient | None = None,
        context_service: ContextService | None = None,
        chat_history_service=None,
        result_repository=None,
        event_service=None,
        route_log_repository=None,
        evidence_provider=None,
        plan_service: PlanService | None = None,
        agent_context_service=None,
        plan_continuation_resolver=None,
        turn_service: TurnService | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.llm_client = llm_client or _llm_client(settings)
        self.context_service = context_service or ContextService(settings)
        self.chat_history_service = chat_history_service
        self.result_repository = result_repository
        self.event_service = event_service
        self.route_log_repository = route_log_repository
        self.evidence_provider = evidence_provider
        self.plan_service = plan_service
        self.agent_context_service = agent_context_service
        self.plan_continuation_resolver = plan_continuation_resolver
        self.turn_service = turn_service

    async def route(self, request: RouteRequest) -> RouteResponse:
        request_id = request.request_id or f"req_{uuid4().hex}"
        request = request.model_copy(update={"request_id": request_id})
        await self._start_turn(request)
        available_agents = await self._available_agent_definitions(request)
        available_agent_ids = [agent.agent_id for agent in available_agents]
        tag_filter = _filter_agents_by_tags(request.input.text, available_agents)
        candidate_ids = available_agent_ids
        candidates = [agent.to_candidate() for agent in available_agents]
        if not available_agents:
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
                context=RouteContext(
                    relation="unsupported",
                    current_agent_id=(
                        request.current_agent.agent_id if request.current_agent else None
                    ),
                    candidate_agent_ids=[],
                    metadata={
                        "authorization_filtered": True,
                        "tag_filter": "no_available_agents",
                        "available_agent_ids": [],
                    },
                ),
            )
            response = self._finalize_assistant_message(response)
            return await self._after_route(request, response)
        host_history = await self._host_history(request)
        agent_history = await self._agent_history(request)
        recent_results = await self._recent_results(request)
        recent_events = await self._recent_events(request)
        active_plan = await self._active_plan(request)
        if active_plan is not None:
            self._validate_plan_agents(active_plan, set(candidate_ids))
        evidence_result = await self._evidence(request, candidate_ids)
        if evidence_result.route_override_denied:
            return await self._route_from_denied_evidence_override(
                request,
                request_id,
                candidate_ids,
                evidence_result,
                tag_filter,
                available_agent_ids,
                host_history=host_history,
                agent_history=agent_history,
                recent_results=recent_results,
                recent_events=recent_events,
                active_plan=active_plan,
                candidate_agents=candidates,
            )
        if evidence_result.route_override:
            return await self._route_from_evidence_override(
                request,
                request_id,
                candidate_ids,
                evidence_result,
                tag_filter,
                available_agent_ids,
                host_history=host_history,
                agent_history=agent_history,
                recent_results=recent_results,
                recent_events=recent_events,
                active_plan=active_plan,
                candidate_agents=candidates,
            )
        (
            base_context,
            projection,
            assembly_session,
        ) = await self.context_service.assemble_route_context(
            request,
            candidate_agent_ids=candidate_ids,
            candidate_agents=candidates,
            request_id=request_id,
            host_history=host_history,
            agent_history=agent_history,
            recent_results=recent_results,
            recent_events=recent_events,
            evidence=evidence_result.evidence,
            active_plan=active_plan,
            intent_hint=evidence_result.intent_hint,
        )
        base_context = _with_evidence_metadata(base_context, evidence_result)
        base_context = _with_filter_metadata(base_context, tag_filter, available_agent_ids)
        knowledge_reply = base_context.metadata.get("knowledge_direct_reply")
        if isinstance(knowledge_reply, dict) and knowledge_reply.get("message"):
            message = str(knowledge_reply["message"])
            response = RouteResponse(
                request_id=request_id,
                session_id=request.session_id,
                assistant_message=message,
                decision=RouteDecision(
                    status="ok",
                    action="reply",
                    confidence=float(knowledge_reply.get("score") or 1.0),
                    reason="Governed route knowledge supplied a reliable direct answer.",
                    message=message,
                ),
                context=base_context,
            )
            response = self._finalize_assistant_message(response)
            response = await self._after_route(request, response)
            return response

        output = await self.llm_client.route(
            LLMRouteInput(
                request=request,
                candidates=candidates,
                context=base_context,
                projection=(
                    projection if self.settings.context_pipeline_mode == "enforced" else None
                ),
            )
        )
        output = self._bind_plan_ownership(output, request)
        output = self._normalize_candidate_context(output, base_context, candidate_ids)
        output = self._ensure_plan_for_multi_task(output, request, candidates)
        output = self._collapse_single_step_plan(output, request)
        output = await self._apply_plan_policy(output)
        output = output.model_copy(update={"request_id": request_id})
        output = await self._post_validate(output, request)
        output = self._clarify_on_low_confidence(output)
        response = await self._clarify_or_attach_invocation(
            output,
            request,
            active_plan=active_plan,
            assembly_session=assembly_session,
        )
        response = self._finalize_assistant_message(response)
        response = await self._after_route(request, response)
        return response

    async def route_decision_shadow(self, request: RouteRequest) -> RouteResponse:
        shadow = copy(self)
        shadow.turn_service = None
        shadow.plan_service = None
        shadow.chat_history_service = None
        shadow.result_repository = None
        shadow.event_service = None
        shadow.route_log_repository = None
        shadow.agent_context_service = None
        shadow.plan_continuation_resolver = None
        return await shadow.route(request)

    async def _start_turn(self, request: RouteRequest) -> None:
        if self.turn_service is None:
            return
        tenant_id = request.user.tenant_id
        if not tenant_id:
            raise RoutingError("Trusted tenant identity is required")
        await self.turn_service.start_turn(
            tenant_id=tenant_id,
            user_id=request.user.id,
            session_id=request.session_id,
            request_id=request.request_id or "",
            source=request.source,
            user_input=TurnUserInput(
                text=request.input.text,
                metadata={
                    "input_type": request.input.type,
                    "attachment_count": len(request.input.attachments),
                },
            ),
        )

    async def _available_agent_definitions(self, request: RouteRequest) -> list[AgentDefinition]:
        return [
            agent
            for agent in await self.registry.list_definitions(enabled_only=True)
            if agent.is_available_to(request.user)
        ]

    async def _post_validate(self, output: RouteResponse, request: RouteRequest) -> RouteResponse:
        candidate_ids = set(output.context.candidate_agent_ids)
        if output.decision.target_agent_id and output.decision.target_agent_id not in candidate_ids:
            raise RoutingError("Router selected an Agent outside the candidate set")
        if output.decision.action == "continue_agent":
            current = request.current_agent.agent_id if request.current_agent else None
            if output.decision.target_agent_id != current:
                raise RoutingError("continue_agent target must match current Agent")
        if output.plan is not None:
            self._validate_plan_agents(output.plan, candidate_ids)
        try:
            return RouteResponse.model_validate(output.model_dump())
        except ValidationError as exc:
            raise RoutingError(
                "Router output validation failed", details={"errors": exc.errors()}
            ) from exc

    @staticmethod
    def _validate_plan_agents(plan, candidate_ids: set[str]) -> None:
        unauthorized = sorted(
            {step.agent_id for step in plan.steps if step.agent_id not in candidate_ids}
        )
        if unauthorized:
            raise RoutingError(
                "Plan contains an Agent outside the candidate set",
                details={"unauthorized_agent_ids": unauthorized},
            )

    def _normalize_candidate_context(
        self,
        output: RouteResponse,
        base_context: RouteContext,
        candidate_agent_ids: list[str],
    ) -> RouteResponse:
        metadata = {**base_context.metadata, **output.context.metadata}
        context = output.context.model_copy(
            update={
                "candidate_agent_ids": candidate_agent_ids,
                "evidence": base_context.evidence,
                "intent_hint": output.context.intent_hint or base_context.intent_hint,
                "metadata": metadata,
            }
        )
        return output.model_copy(update={"context": context})

    def _clarify_on_low_confidence(self, output: RouteResponse) -> RouteResponse:
        threshold = self.settings.router_low_confidence_threshold
        confidence = output.decision.confidence
        if threshold <= 0 or confidence is None or confidence >= threshold:
            return output
        if output.decision.action in {"clarify", "unsupported", "silent", "exit_agent"}:
            return output

        reason = "Route confidence is below the clarification threshold."
        metadata = {
            **output.context.metadata,
            "low_confidence": {
                "confidence": confidence,
                "threshold": threshold,
                "reason": reason,
            },
        }
        context = output.context.model_copy(update={"metadata": metadata})
        return output.model_copy(
            update={
                "decision": RouteDecision(
                    status="clarify",
                    action="clarify",
                    confidence=confidence,
                    reason=reason,
                    message="我还不确定该交给哪个 Agent 处理，请补充一下目标或关键信息。",
                ),
                "context": context,
                "execution_policy": None,
                "next_action": None,
                "plan": None,
                "invocation": None,
            }
        )

    async def _clarify_or_attach_invocation(
        self,
        output: RouteResponse,
        request: RouteRequest,
        *,
        active_plan=None,
        assembly_session=None,
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
            message = f"请补充以下信息后再继续：{', '.join(missing)}。"
            metadata = {
                **output.context.metadata,
                "missing_required_inputs": {
                    "agent_id": agent.agent_id,
                    "fields": missing,
                },
            }
            return output.model_copy(
                update={
                    "decision": RouteDecision(
                        status="clarify",
                        action="clarify",
                        confidence=output.decision.confidence,
                        reason=f"Missing required inputs: {', '.join(missing)}",
                        message=message,
                    ),
                    "context": output.context.model_copy(update={"metadata": metadata}),
                    "next_action": NextAction(
                        type="collect_input",
                        message=message,
                        agent_id=agent.agent_id,
                        params={"missing_inputs": missing},
                        metadata={"missing_inputs": missing, "target_agent_id": agent.agent_id},
                    ),
                    "invocation": None,
                }
            )
        metadata = {}
        context = output.context
        execution_plan = (
            active_plan
            if active_plan is not None
            and (request.plan_id or requests_plan_continuation(request.input.text))
            else None
        )
        if self.agent_context_service:
            runtime_context = await self.agent_context_service.assemble_for_route(
                agent=agent,
                request=request,
                invocation_input=invocation_input,
                active_plan=execution_plan,
                assembly_session=assembly_session,
            )
            metadata = {
                "memory_context_status": runtime_context.memory_context.status,
                "knowledge_context_status": runtime_context.knowledge_context.status,
            }
            if execution_plan is not None:
                metadata["plan_id"] = execution_plan.plan_id
            context = output.context.model_copy(
                update={
                    "metadata": {
                        **output.context.metadata,
                        "agent_context": {
                            "agent_id": agent.agent_id,
                            "memory_context_status": runtime_context.memory_context.status,
                            "knowledge_context_status": runtime_context.knowledge_context.status,
                            "memory_item_count": len(runtime_context.memory_context.items),
                            "knowledge_item_count": len(runtime_context.knowledge_context.items),
                        },
                    }
                }
            )
        return output.model_copy(
            update={
                "context": context,
                "invocation": InvocationPreview(
                    mode="deferred",
                    agent_id=agent.agent_id,
                    input=invocation_input,
                    metadata=metadata,
                ),
            }
        )

    def _finalize_assistant_message(self, response: RouteResponse) -> RouteResponse:
        assistant_message = _clean_user_text(response.assistant_message)
        if not assistant_message:
            assistant_message = _assistant_message_from_route(response)
        decision = response.decision
        if not _clean_user_text(decision.message) and assistant_message:
            decision = decision.model_copy(update={"message": assistant_message})
        return response.model_copy(
            update={
                "assistant_message": assistant_message,
                "decision": decision,
            }
        )

    async def _host_history(self, request: RouteRequest) -> list[dict]:
        if not self.chat_history_service:
            return []
        return [
            item.model_dump()
            for item in await self.chat_history_service.get_host_history(
                request.session_id,
                tenant_id=request.user.tenant_id or "",
                user_id=request.user.id,
            )
        ]

    async def _agent_history(self, request: RouteRequest) -> list[dict]:
        if not self.chat_history_service or not request.current_agent:
            return []
        return [
            item.model_dump()
            for item in await self.chat_history_service.get_agent_history(
                request.session_id,
                request.current_agent.agent_id,
                tenant_id=request.user.tenant_id or "",
                user_id=request.user.id,
            )
        ]

    async def _recent_results(self, request: RouteRequest) -> list[dict]:
        if not self.result_repository:
            return []
        return [
            item.model_dump()
            for item in await self.result_repository.list_recent(
                request.session_id,
                tenant_id=request.user.tenant_id or "",
                user_id=request.user.id,
                limit=self.settings.router_max_recent_results,
            )
        ]

    async def _recent_events(self, request: RouteRequest) -> list[dict]:
        if not self.event_service:
            return []
        events = await self.event_service.list_recent_events(
            request.session_id,
            tenant_id=request.user.tenant_id or "",
            user_id=request.user.id,
            limit=self.settings.router_max_recent_events,
        )
        if request.event_id:
            referenced = await self.event_service.get_event(
                request.event_id,
                tenant_id=request.user.tenant_id,
                user_id=request.user.id,
            )
            if referenced is not None:
                events = [
                    referenced,
                    *[item for item in events if item.event_id != referenced.event_id],
                ]
        return [item.model_dump() for item in events[: self.settings.router_max_recent_events]]

    async def _active_plan(self, request: RouteRequest):
        if not self.plan_service:
            return None
        tenant_id = request.user.tenant_id or ""
        if request.plan_id:
            return await self.plan_service.get_plan(
                request.plan_id,
                tenant_id=tenant_id,
                user_id=request.user.id,
            )
        active = await self.plan_service.get_active_plan(
            request.session_id,
            tenant_id=tenant_id,
            user_id=request.user.id,
        )
        if active is not None or self.plan_continuation_resolver is None:
            return active
        return await self.plan_continuation_resolver.resolve(request)

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
        tag_filter: "TagFilterResult",
        available_agent_ids: list[str],
        *,
        host_history: list[JsonDict] | None = None,
        agent_history: list[JsonDict] | None = None,
        recent_results: list[JsonDict] | None = None,
        recent_events: list[JsonDict] | None = None,
        active_plan=None,
        candidate_agents=None,
    ) -> RouteResponse:
        override = evidence_result.route_override or {}
        target_agent_id = override.get("target_agent_id")
        if target_agent_id and target_agent_id not in candidate_agent_ids:
            evidence_result.route_override_denied = {
                **override,
                "target_agent_id": target_agent_id,
                "reason": "permission_denied",
            }
            evidence_result.route_override = None
            return await self._route_from_denied_evidence_override(
                request,
                request_id,
                candidate_agent_ids,
                evidence_result,
                tag_filter,
                available_agent_ids,
                host_history=host_history,
                agent_history=agent_history,
                recent_results=recent_results,
                recent_events=recent_events,
                active_plan=active_plan,
                candidate_agents=candidate_agents,
            )
        base_context, _, assembly_session = await self.context_service.assemble_route_context(
            request,
            candidate_agent_ids=candidate_agent_ids,
            candidate_agents=candidate_agents or [],
            request_id=request_id,
            host_history=host_history,
            agent_history=agent_history,
            recent_results=recent_results,
            recent_events=recent_events,
            evidence=evidence_result.evidence,
            active_plan=active_plan,
            intent_hint=evidence_result.intent_hint,
        )
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
            context=base_context,
        )
        response = response.model_copy(
            update={"context": _with_evidence_metadata(response.context, evidence_result)}
        )
        response = response.model_copy(
            update={
                "context": _with_filter_metadata(
                    response.context,
                    tag_filter,
                    available_agent_ids,
                )
            }
        )
        response = await self._clarify_or_attach_invocation(
            response,
            request,
            active_plan=active_plan,
            assembly_session=assembly_session,
        )
        response = self._finalize_assistant_message(response)
        response = await self._after_route(request, response)
        return response

    async def _route_from_denied_evidence_override(
        self,
        request: RouteRequest,
        request_id: str,
        candidate_agent_ids: list[str],
        evidence_result,
        tag_filter: "TagFilterResult",
        available_agent_ids: list[str],
        *,
        host_history: list[JsonDict] | None = None,
        agent_history: list[JsonDict] | None = None,
        recent_results: list[JsonDict] | None = None,
        recent_events: list[JsonDict] | None = None,
        active_plan=None,
        candidate_agents=None,
    ) -> RouteResponse:
        denied = evidence_result.route_override_denied or {}
        target_agent_id = denied.get("target_agent_id")
        message = "你当前无权限使用该能力，请联系管理员开通权限。"
        context, _, _ = await self.context_service.assemble_route_context(
            request,
            candidate_agent_ids=candidate_agent_ids,
            candidate_agents=candidate_agents or [],
            request_id=request_id,
            host_history=host_history,
            agent_history=agent_history,
            recent_results=recent_results,
            recent_events=recent_events,
            evidence=evidence_result.evidence,
            active_plan=active_plan,
            intent_hint=evidence_result.intent_hint,
        )
        context = context.model_copy(
            update={
                "relation": "unsupported",
                "metadata": {
                    **context.metadata,
                    "permission_denied": True,
                    "route_override_denied": {
                        "target_agent_id": target_agent_id,
                        "reason": denied.get("reason", "permission_denied"),
                    },
                },
            }
        )
        context = _with_evidence_metadata(context, evidence_result)
        context = _with_filter_metadata(context, tag_filter, available_agent_ids)
        response = RouteResponse(
            request_id=request_id,
            session_id=request.session_id,
            assistant_message=message,
            decision=RouteDecision(
                status="unsupported",
                action="unsupported",
                confidence=1.0,
                reason="Matched fixed-question route override but target Agent is unavailable.",
                message=message,
            ),
            context=context,
        )
        response = self._finalize_assistant_message(response)
        response = await self._after_route(request, response)
        return response

    async def _after_route(self, request: RouteRequest, response: RouteResponse) -> RouteResponse:
        if self.plan_service and response.plan is not None:
            await self.plan_service.save_plan(response.plan)
        if self.turn_service and response.plan is not None:
            tenant_id = request.user.tenant_id
            if not tenant_id:
                raise RoutingError("Trusted tenant identity is required")
            await self.turn_service.attach_activity(
                tenant_id=tenant_id,
                user_id=request.user.id,
                request_id=response.request_id,
                plan_id=response.plan.plan_id,
                blocked=response.execution_policy != "auto_execute",
            )
        if (
            self.turn_service
            and response.plan is None
            and response.invocation is None
            and response.decision.action in {"reply", "clarify", "unsupported", "silent"}
        ):
            tenant_id = request.user.tenant_id
            if not tenant_id:
                raise RoutingError("Trusted tenant identity is required")
            await self.turn_service.complete_route_only(
                tenant_id=tenant_id,
                user_id=request.user.id,
                request_id=response.request_id,
                response_kind=response.decision.action,
                response_text=response.assistant_message or response.decision.message or "",
                error=response.error.model_dump(mode="json") if response.error else None,
            )
        if self.chat_history_service:
            await self.chat_history_service.record_user_input(
                session_id=request.session_id,
                user_id=request.user.id,
                tenant_id=request.user.tenant_id or "",
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

            context_pack_summary = self.context_service.context_pack_log_summary(
                response.context.metadata.get("context_pack")
            )
            try:
                await self.route_log_repository.add(
                    RouteLog(
                        request_id=response.request_id,
                        session_id=response.session_id,
                        model_name=self.settings.router_llm_model,
                        candidate_agent_ids=response.context.candidate_agent_ids,
                        prompt_summary=request.input.text[:500],
                        evidence=response.context.evidence,
                        parsed_output={
                            **_route_log_response(response, context_pack_summary),
                            "context_pack_usage": context_pack_summary,
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
            except Exception as exc:
                trace = response.context.metadata.get("context_trace")
                trace = dict(trace) if isinstance(trace, dict) else {}
                trace["persistence"] = {
                    "status": "error",
                    "error_code": "route_log_persistence_failed",
                    "error_type": type(exc).__name__,
                }
                response = response.model_copy(
                    update={
                        "context": response.context.model_copy(
                            update={
                                "metadata": {
                                    **response.context.metadata,
                                    "context_trace": trace,
                                }
                            }
                        )
                    }
                )
        return response

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
            user_id=request.user.id,
            tenant_id=request.user.tenant_id or "",
            candidates=candidates,
        )
        if plan is None:
            return output
        action = output.decision.action
        if action in {"open_agent", "continue_agent"}:
            action = "reply"
        return output.model_copy(
            update={
                "decision": RouteDecision(
                    status=output.decision.status,
                    action=action,
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

    def _bind_plan_ownership(self, output: RouteResponse, request: RouteRequest) -> RouteResponse:
        if output.plan is None:
            return output
        tenant_id = request.user.tenant_id
        if not tenant_id:
            raise RoutingError("Trusted tenant identity is required to create a Plan")
        return output.model_copy(
            update={
                "plan": output.plan.model_copy(
                    update={
                        "user_id": request.user.id,
                        "tenant_id": tenant_id,
                        "last_event_id": None,
                        "state_version": 0,
                    }
                )
            }
        )

    def _collapse_single_step_plan(
        self,
        output: RouteResponse,
        request: RouteRequest,
    ) -> RouteResponse:
        if output.plan is None or len(output.plan.steps) != 1:
            return output
        step = output.plan.steps[0]
        if step.agent_id not in output.context.candidate_agent_ids:
            return output

        current_agent_id = request.current_agent.agent_id if request.current_agent else None
        action = "continue_agent" if current_agent_id == step.agent_id else "open_agent"
        relation = "continue_current" if current_agent_id == step.agent_id else "new_task"
        return output.model_copy(
            update={
                "decision": RouteDecision(
                    status=output.decision.status,
                    action=action,
                    target_agent_id=step.agent_id,
                    confidence=output.decision.confidence,
                    reason=output.decision.reason or "Collapsed single-step plan.",
                    message=f"Routing to {step.agent_id}.",
                ),
                "context": output.context.model_copy(update={"relation": relation}),
                "execution_policy": None,
                "next_action": None,
                "plan": None,
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


@dataclass
class TagFilterResult:
    agents: list[AgentDefinition]
    status: str
    matched_agent_ids: list[str]
    matches: dict[str, list[str]]


def _filter_agents_by_tags(text: str, agents: list[AgentDefinition]) -> TagFilterResult:
    if not agents:
        return TagFilterResult(
            agents=[],
            status="no_available_agents",
            matched_agent_ids=[],
            matches={},
        )

    normalized_text = _normalize_text(text)
    matches: dict[str, list[str]] = {}
    for agent in agents:
        matched_terms = [
            term
            for term in _agent_filter_terms(agent)
            if _matches_filter_term(normalized_text, term)
        ]
        if matched_terms:
            matches[agent.agent_id] = sorted(set(matched_terms), key=str.lower)

    if not matches:
        return TagFilterResult(
            agents=agents,
            status="no_match_no_filter",
            matched_agent_ids=[],
            matches={},
        )

    matched_ids = set(matches)
    matched_agent_ids = [agent.agent_id for agent in agents if agent.agent_id in matched_ids]
    return TagFilterResult(
        agents=agents,
        status="matched_but_not_applied",
        matched_agent_ids=matched_agent_ids,
        matches=matches,
    )


def _agent_filter_terms(agent: AgentDefinition) -> list[str]:
    terms: list[str] = []
    terms.extend(agent.tags)
    terms.extend(agent.capabilities)
    terms.extend(agent.trigger.keywords)
    terms.extend(agent.trigger.positive_examples)
    terms.extend(_metadata_terms(agent.metadata.get("intent_tags")))
    terms.extend(_metadata_terms(agent.metadata.get("routing_tags")))
    return [_normalize_text(term) for term in terms if _normalize_text(term)]


def _metadata_terms(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if item is not None]
    return []


def _matches_filter_term(normalized_text: str, normalized_term: str) -> bool:
    if not normalized_text or not normalized_term:
        return False
    if normalized_term in normalized_text:
        return True
    term_tokens = _tokens(normalized_term)
    if not term_tokens:
        return False
    return any(_contains_token(normalized_text, token) for token in term_tokens)


def _with_filter_metadata(
    context: RouteContext,
    tag_filter: TagFilterResult,
    available_agent_ids: list[str],
) -> RouteContext:
    effective_candidate_ids = [agent.agent_id for agent in tag_filter.agents]
    metadata = {
        **context.metadata,
        "available_agent_ids": available_agent_ids,
        "filtered_candidate_agent_ids": effective_candidate_ids,
        "tag_filter": tag_filter.status,
        "tag_filter_applied": False,
        "tag_filter_matched_agent_ids": tag_filter.matched_agent_ids,
    }
    if tag_filter.status == "matched_but_not_applied":
        metadata["tag_filter_matches"] = tag_filter.matches
        metadata["tag_filter_reason"] = "matched_but_not_applied"
    elif tag_filter.status == "no_match_no_filter":
        metadata["tag_filter_reason"] = "no_match_no_filter"
    return context.model_copy(update={"metadata": metadata})


def _with_evidence_metadata(context: RouteContext, evidence_result) -> RouteContext:
    metadata = {**context.metadata}
    if evidence_result.candidate_agent_ids:
        metadata["evidence_candidate_agent_ids"] = evidence_result.candidate_agent_ids
    if evidence_result.errors:
        metadata["evidence_errors"] = evidence_result.errors
    if getattr(evidence_result, "metadata", None):
        metadata["evidence_provider_metadata"] = evidence_result.metadata
    if evidence_result.route_override_denied:
        denied = evidence_result.route_override_denied
        metadata["route_override_denied"] = {
            "target_agent_id": denied.get("target_agent_id"),
            "reason": denied.get("reason", "permission_denied"),
        }
    return context.model_copy(update={"metadata": metadata})


def _route_log_response(
    response: RouteResponse,
    context_pack_summary: JsonDict | None,
) -> JsonDict:
    payload = response.model_dump(mode="json")
    context = payload.get("context")
    if isinstance(context, dict):
        metadata = context.get("metadata")
        if isinstance(metadata, dict):
            metadata = {**metadata}
            for key in ["host_history", "agent_history", "recent_results", "recent_events"]:
                value = metadata.pop(key, None)
                if isinstance(value, list):
                    metadata[f"{key}_count"] = len(value)
            frontend_context = metadata.pop("frontend_context", None)
            if isinstance(frontend_context, dict):
                metadata["frontend_context_keys"] = sorted(
                    str(key) for key in frontend_context.keys()
                )[:20]
            metadata.pop("active_plan", None)
            if "context_pack" in metadata:
                metadata["context_pack"] = context_pack_summary
            context["metadata"] = redact_value(metadata)
    return redact_value(payload)


def _assistant_message_from_route(response: RouteResponse) -> str:
    decision_message = _clean_user_text(response.decision.message)
    if decision_message:
        return decision_message
    if response.decision.action == "clarify":
        return "请补充必要信息后再继续。"
    if response.plan is not None:
        return "已生成多步骤执行计划，请在计划面板中确认下一步。"
    if response.decision.action == "open_agent":
        target = response.decision.target_agent_id or "目标 Agent"
        return f"已为你路由到 {target}。"
    if response.decision.action == "continue_agent":
        target = response.decision.target_agent_id or "当前 Agent"
        return f"继续由 {target} 处理。"
    if response.decision.action == "exit_agent":
        return "已退出当前 Agent。"
    if response.decision.action == "unsupported":
        return "当前没有可用 Agent 可以处理这个请求。"
    if response.decision.action == "reply":
        return "已收到。"
    if response.decision.action == "silent":
        return ""
    return "路由完成。"


def _clean_user_text(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _missing_required_inputs(agent: AgentDefinition, invocation_input: dict) -> list[str]:
    return missing_required_inputs(agent, invocation_input)


def _build_invocation_input(
    agent: AgentDefinition,
    output: RouteResponse,
    request: RouteRequest,
) -> dict:
    return build_invocation_input(agent, request.input.text)


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower().replace("_", " "))


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text) if len(token) >= 2]


def _contains_token(normalized_text: str, token: str) -> bool:
    if re.fullmatch(r"[0-9a-zA-Z]+", token):
        return re.search(rf"\b{re.escape(token)}\b", normalized_text) is not None
    return token in normalized_text


def _llm_client(settings: Settings) -> LLMClient:
    if settings.router_llm_provider == "mock":
        return MockLLMClient()
    return OpenAICompatibleLLMClient(settings)
