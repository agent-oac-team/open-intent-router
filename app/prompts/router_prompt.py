import json
from pathlib import Path

import yaml

from app.schemas.routing import LLMRouteInput

DEFAULT_SYSTEM_PROMPT = (
    "你是一个意图识别与 Agent 路由器。"
    "你必须只返回符合 response_schema_hint 的严格 JSON。"
    "target_agent_id 只能从 candidate_agent_ids 中选择。"
)

DEFAULT_USER_TEMPLATE = (
    "请根据 request、candidate_agents 和 context 进行意图识别与路由决策。\n\n"
    "输入数据：\n{payload_json}\n\n"
    "输出要求：只返回 JSON，不要返回 Markdown、解释或代码块。"
)


class RouterPromptTemplate:
    def __init__(
        self,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        user_template: str = DEFAULT_USER_TEMPLATE,
    ) -> None:
        self.system_prompt = system_prompt
        self.user_template = user_template

    @classmethod
    def from_file(cls, path: str | Path | None) -> "RouterPromptTemplate":
        if not path:
            return cls()
        prompt_path = Path(path)
        if not prompt_path.exists():
            return cls()
        data = yaml.safe_load(prompt_path.read_text(encoding="utf-8")) or {}
        return cls(
            system_prompt=data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            user_template=data.get("user_template") or DEFAULT_USER_TEMPLATE,
        )

    def messages(self, payload: LLMRouteInput) -> list[dict[str, str]]:
        prompt_payload = {
            "request": payload.request.model_dump(mode="json"),
            "candidate_agents": [agent.model_dump(mode="json") for agent in payload.candidates],
            "context": payload.context.model_dump(mode="json"),
            "response_schema_hint": route_response_schema_hint(
                [agent.agent_id for agent in payload.candidates]
            ),
            "routing_rules": route_response_rules(),
        }
        payload_json = json.dumps(prompt_payload, ensure_ascii=False)
        return [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": self.user_template.replace("{payload_json}", payload_json),
            },
        ]


def route_response_schema_hint(candidate_agent_ids: list[str]) -> dict:
    return {
        "request_id": "string",
        "session_id": "string",
        "assistant_message": "string|null; primary user-visible chat text",
        "decision": {
            "status": "ok|clarify|unsupported|error",
            "action": "reply|clarify|open_agent|continue_agent|exit_agent|show_plan|unsupported|silent",
            "target_agent_id": "string|null",
            "confidence": "number|null",
            "reason": "string",
            "message": "string",
        },
        "context": {
            "relation": "new_task|continue_current|switch_agent|exit_agent|multi_task|unsupported",
            "current_agent_id": "string|null",
            "artifact_refs": [],
            "candidate_agent_ids": candidate_agent_ids,
            "intent_hint": "string|null",
            "evidence": [],
            "metadata": {},
        },
        "execution_policy": "return_plan_only|require_confirmation|auto_execute|host_managed|null",
        "next_action": {
            "type": "confirm_plan|open_ui|collect_input|wait_for_agent_event|none",
            "message": "string",
            "agent_id": "string|null",
            "plan_id": "string|null",
            "step_id": "string|null",
            "route": "string|null",
            "params": {},
            "metadata": {},
        },
        "plan": {
            "plan_id": "string|null; required when plan is present, may be omitted and server will fill",
            "session_id": "string|null",
            "status": "pending|running|blocked|completed|failed|cancelled",
            "current_step_id": "string|null",
            "execution_policy": "return_plan_only|require_confirmation|auto_execute|host_managed|null",
            "next_action": "same shape as top-level next_action|null",
            "steps": [
                {
                    "step_id": "string",
                    "agent_id": f"one of {candidate_agent_ids}",
                    "description": "string",
                    "status": "pending|running|blocked|completed|failed|cancelled",
                    "depends_on": ["previous_step_id"],
                    "artifact_refs": [],
                }
            ],
        },
        "invocation": None,
    }


def route_response_rules() -> list[str]:
    return [
        "Single-agent requests should use open_agent or continue_agent and plan must be null.",
        "assistant_message is the primary chat text. Keep it concise and user-visible.",
        "decision.message is compatibility route-stage text; decision.reason is for debug or route explanation.",
        "next_action.message belongs to plan or host collaboration status and must not be appended to assistant_message.",
        (
            "Do not create a plan for a single intent such as 'summarize this text'; "
            "route it to the matching single Agent instead."
        ),
        "For normal single-agent routing, execution_policy and next_action must be null.",
        (
            "Multi-intent or ordered requests such as 'first summarize, then create a task' "
            "must set context.relation=multi_task, target_agent_id=null, invocation=null, "
            "and include plan.steps."
        ),
        (
            "For multi-intent requests, plan presence is the primary contract. "
            "action=show_plan is accepted for compatibility but is not required."
        ),
        "If execution_policy=require_confirmation, include next_action.type=confirm_plan.",
        "Every plan step agent_id must be selected from candidate_agent_ids.",
        "Use dependency edges in depends_on when a later step needs the previous step output.",
    ]
