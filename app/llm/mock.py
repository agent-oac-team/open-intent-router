from uuid import uuid4

from app.schemas.routing import LLMRouteInput, RouteContext, RouteDecision, RouteResponse
from app.services.plan_builder import build_ordered_plan_from_text


class MockLLMClient:
    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        text = payload.request.input.text.lower()
        candidates = payload.candidates
        candidate_ids = [agent.agent_id for agent in candidates]
        plan = build_ordered_plan_from_text(
            text=payload.request.input.text,
            session_id=payload.request.session_id,
            candidates=candidates,
        )

        if plan is not None:
            return RouteResponse(
                request_id=payload.request.request_id or f"req_{uuid4().hex}",
                session_id=payload.request.session_id,
                assistant_message="已生成多步骤执行计划，请确认后继续。",
                decision=RouteDecision(
                    status="ok",
                    action="reply",
                    target_agent_id=None,
                    confidence=0.6,
                    reason="Mock router detected an ordered multi-agent task.",
                    message="已生成多步骤执行计划。",
                ),
                context=RouteContext(
                    relation="multi_task",
                    current_agent_id=(
                        payload.request.current_agent.agent_id
                        if payload.request.current_agent
                        else None
                    ),
                    candidate_agent_ids=candidate_ids,
                    artifact_refs=[],
                    intent_hint=payload.context.intent_hint,
                    evidence=payload.context.evidence,
                ),
                plan=plan,
                invocation=None,
            )

        selected = None
        for agent in candidates:
            tokens = _agent_tokens(agent)
            if any(token in text for token in tokens):
                selected = agent
                break

        if selected is None and candidates:
            selected = candidates[0]

        if selected is None:
            decision = RouteDecision(
                status="unsupported",
                action="unsupported",
                confidence=0.0,
                reason="No candidate Agent is available.",
                message="No available Agent can handle this request.",
            )
        else:
            decision = RouteDecision(
                status="ok",
                action="open_agent",
                target_agent_id=selected.agent_id,
                confidence=0.5,
                reason="Mock router selected a candidate Agent.",
                message=f"Routing to {selected.name}.",
            )

        return RouteResponse(
            request_id=payload.request.request_id or f"req_{uuid4().hex}",
            session_id=payload.request.session_id,
            assistant_message=decision.message,
            decision=decision,
            context=RouteContext(
                relation="new_task",
                current_agent_id=(
                    payload.request.current_agent.agent_id
                    if payload.request.current_agent
                    else None
                ),
                candidate_agent_ids=candidate_ids,
                artifact_refs=[],
                intent_hint=payload.context.intent_hint,
                evidence=payload.context.evidence,
            ),
            plan=None,
            invocation=None,
        )


def _agent_tokens(agent) -> list[str]:
    raw_tokens = [
        *agent.capabilities,
        *agent.trigger.keywords,
        *agent.trigger.positive_examples,
        agent.name,
        agent.description,
    ]
    tokens: list[str] = []
    for raw in raw_tokens:
        for token in str(raw).lower().replace("_", " ").split():
            if len(token) >= 4:
                tokens.append(token)
    return tokens
