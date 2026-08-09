import json
import re
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import LLMError
from app.prompts.router_prompt import RouterPromptTemplate
from app.schemas.agents import CandidateAgent
from app.schemas.routing import LLMRouteInput, RouteResponse
from app.services.plan_builder import build_ordered_plan_from_text

PROMPT_ONLY_RESPONSE_KEYS = {"rules", "routing_rules"}


class OpenAICompatibleLLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prompt_template = RouterPromptTemplate.from_file(settings.router_prompt_file)

    async def route(self, payload: LLMRouteInput) -> RouteResponse:
        if not self.settings.router_llm_base_url:
            raise LLMError("ROUTER_LLM_BASE_URL is required for OpenAI-compatible routing")
        if not self.settings.router_llm_api_key:
            raise LLMError("ROUTER_LLM_API_KEY is required for OpenAI-compatible routing")
        if self.settings.router_llm_api_key.strip() in {
            "replace-with-real-key",
            "your-api-key",
            "your-deepseek-api-key",
        }:
            raise LLMError(
                "ROUTER_LLM_API_KEY is still a placeholder; configure a valid provider API key",
                details={"setting": "ROUTER_LLM_API_KEY"},
            )

        url = self.settings.router_llm_base_url.rstrip("/") + "/v1/chat/completions"
        body = {
            "model": self.settings.router_llm_model,
            "messages": self.prompt_template.messages(payload),
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.router_llm_api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.router_llm_timeout_seconds
            ) as client:
                response = await client.post(url, headers=headers, json=body)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(
                "OpenAI-compatible LLM request failed", details={"error": str(exc)}
            ) from exc

        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("OpenAI-compatible LLM returned invalid JSON") from exc
        parsed = _normalize_route_response(parsed, payload)
        try:
            return RouteResponse.model_validate(parsed)
        except ValidationError as exc:
            raise LLMError(
                "OpenAI-compatible LLM returned a response that does not match RouteResponse",
                details={"errors": exc.errors()},
            ) from exc


def _normalize_route_response(parsed: object, payload: LLMRouteInput) -> object:
    if not isinstance(parsed, dict):
        return parsed

    candidate_ids = [agent.agent_id for agent in payload.candidates]
    normalized = dict(parsed)
    for key in PROMPT_ONLY_RESPONSE_KEYS:
        normalized.pop(key, None)
    normalized["request_id"] = (
        normalized.get("request_id") or payload.request.request_id or f"req_{uuid4().hex}"
    )
    normalized["session_id"] = normalized.get("session_id") or payload.request.session_id

    context = normalized.get("context")
    if isinstance(context, dict):
        normalized["context"] = {
            **payload.context.model_dump(mode="json"),
            **context,
            "candidate_agent_ids": context.get("candidate_agent_ids") or candidate_ids,
        }
    else:
        normalized["context"] = {
            **payload.context.model_dump(mode="json"),
            "candidate_agent_ids": candidate_ids,
        }

    decision = normalized.get("decision")
    if isinstance(decision, dict) and (
        decision.get("action") == "show_plan" or _has_plan_steps(normalized.get("plan"))
    ):
        normalized["plan"] = _normalize_plan(normalized.get("plan"), payload)
        if not _has_plan_steps(normalized["plan"]):
            fallback_plan = build_ordered_plan_from_text(
                text=payload.request.input.text,
                session_id=payload.request.session_id,
                user_id=payload.request.user.id,
                tenant_id=payload.request.user.tenant_id or "",
                candidates=payload.candidates,
            )
            if fallback_plan is not None:
                normalized["plan"] = fallback_plan.model_dump(mode="json")
        if not _has_plan_steps(normalized["plan"]):
            recovered = _recover_single_agent_route(normalized, payload)
            if recovered is not None:
                return recovered
        decision["target_agent_id"] = None
        policy = normalized.get("execution_policy") or _plan_policy(normalized["plan"])
        if policy:
            normalized["execution_policy"] = policy
            if isinstance(normalized["plan"], dict):
                normalized["plan"]["execution_policy"] = (
                    normalized["plan"].get("execution_policy") or policy
                )
        if not normalized.get("next_action") and policy in {"require_confirmation", "host_managed"}:
            normalized["next_action"] = {
                "type": "confirm_plan"
                if policy == "require_confirmation"
                else "wait_for_agent_event",
                "message": "请确认是否执行该计划。"
                if policy == "require_confirmation"
                else "该计划由宿主应用继续执行。",
                "plan_id": normalized["plan"].get("plan_id")
                if isinstance(normalized["plan"], dict)
                else None,
            }

    if isinstance(decision, dict) and normalized.get("plan") is None:
        normalized.pop("execution_policy", None)
        if _is_empty_next_action(normalized.get("next_action")):
            normalized.pop("next_action", None)

    return normalized


def _recover_single_agent_route(
    normalized: dict,
    payload: LLMRouteInput,
) -> dict | None:
    decision = normalized.get("decision")
    context = normalized.get("context")
    if not isinstance(decision, dict) or not isinstance(context, dict):
        return None

    agent = _single_agent_candidate(normalized, payload)
    if agent is None:
        return None

    current_agent_id = (
        payload.request.current_agent.agent_id if payload.request.current_agent else None
    )
    action = "continue_agent" if current_agent_id == agent.agent_id else "open_agent"
    relation = "continue_current" if current_agent_id == agent.agent_id else "new_task"
    recovered = dict(normalized)
    recovered["decision"] = {
        **decision,
        "action": action,
        "target_agent_id": agent.agent_id,
        "message": f"Routing to {agent.name}.",
    }
    recovered["context"] = {
        **context,
        "relation": relation,
    }
    recovered["plan"] = None
    recovered["invocation"] = None
    recovered.pop("execution_policy", None)
    recovered.pop("next_action", None)
    return recovered


def _single_agent_candidate(
    normalized: dict,
    payload: LLMRouteInput,
) -> CandidateAgent | None:
    candidate_by_id = {agent.agent_id: agent for agent in payload.candidates}
    decision = normalized.get("decision")
    if isinstance(decision, dict):
        target_agent_id = decision.get("target_agent_id")
        if isinstance(target_agent_id, str) and target_agent_id in candidate_by_id:
            return candidate_by_id[target_agent_id]

    scored = [
        (score, agent)
        for agent in payload.candidates
        if (score := _single_agent_score(payload.request.input.text, agent)) > 0
    ]
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_agent = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if best_score < 40 or best_score == second_score:
        return None
    return best_agent


def _single_agent_score(text: str, agent: CandidateAgent) -> int:
    normalized_text = _normalize_text(text)
    for negative in agent.trigger.negative_examples:
        if _contains_phrase(normalized_text, negative):
            return 0

    score = 0
    for phrase in agent.trigger.positive_examples:
        if _contains_phrase(normalized_text, phrase):
            score += 100
    for phrase in agent.trigger.keywords:
        if _contains_phrase(normalized_text, phrase):
            score += 50
    for phrase in agent.capabilities:
        if _contains_phrase(normalized_text, phrase):
            score += 40
    for phrase in [agent.agent_id, agent.name]:
        if _contains_phrase(normalized_text, phrase):
            score += 30
    for token in _tokens(agent.description):
        if re.search(rf"\b{re.escape(token)}\b", normalized_text):
            score += 3
    return score


def _contains_phrase(normalized_text: str, phrase: str) -> bool:
    normalized_phrase = _normalize_text(phrase)
    if not normalized_phrase:
        return False
    if " " in normalized_phrase:
        return normalized_phrase in normalized_text
    return re.search(rf"\b{re.escape(normalized_phrase)}\b", normalized_text) is not None


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", _normalize_text(text))
        if len(token) >= 4
    ]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower().replace("_", " "))


def _normalize_plan(plan: object, payload: LLMRouteInput) -> object:
    if not isinstance(plan, dict):
        return plan
    normalized = dict(plan)
    normalized["plan_id"] = normalized.get("plan_id") or f"plan_{uuid4().hex}"
    normalized["user_id"] = payload.request.user.id
    normalized["tenant_id"] = payload.request.user.tenant_id or ""
    normalized["session_id"] = normalized.get("session_id") or payload.request.session_id
    normalized["status"] = normalized.get("status") or "pending"
    steps = normalized.get("steps")
    if isinstance(steps, list):
        normalized_steps = []
        for index, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict):
                normalized_steps.append(raw_step)
                continue
            step = dict(raw_step)
            step["step_id"] = step.get("step_id") or f"step_{index}"
            step["description"] = step.get("description") or step["step_id"]
            step["status"] = step.get("status") or "pending"
            step["depends_on"] = step.get("depends_on") or []
            step["artifact_refs"] = step.get("artifact_refs") or []
            normalized_steps.append(step)
        normalized["steps"] = normalized_steps
        if normalized_steps and not normalized.get("current_step_id"):
            first_step = normalized_steps[0]
            if isinstance(first_step, dict):
                normalized["current_step_id"] = first_step.get("step_id")
    return normalized


def _has_plan_steps(plan: object) -> bool:
    return isinstance(plan, dict) and isinstance(plan.get("steps"), list) and len(plan["steps"]) > 0


def _plan_policy(plan: object) -> str | None:
    if isinstance(plan, dict):
        value = plan.get("execution_policy")
        return str(value) if value else None
    return None


def _is_empty_next_action(next_action: object) -> bool:
    if not isinstance(next_action, dict):
        return False
    return (
        next_action.get("type") in {None, "none"}
        and not next_action.get("agent_id")
        and not next_action.get("plan_id")
        and not next_action.get("step_id")
        and not next_action.get("route")
    )
