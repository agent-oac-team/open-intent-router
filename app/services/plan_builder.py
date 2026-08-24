import re
from uuid import uuid4

from app.schemas.agents import CandidateAgentV2
from app.schemas.plans import Plan, PlanStep

ORDERING_MARKERS = (
    "first",
    "then",
    "after",
    "after that",
    "and then",
    "next",
    "先",
    "再",
    "然后",
    "之后",
    "接着",
)


def build_ordered_plan_from_text(
    *,
    text: str,
    session_id: str,
    user_id: str,
    tenant_id: str,
    candidates: list[CandidateAgentV2],
) -> Plan | None:
    lowered = text.lower()
    if not any(marker in lowered for marker in ORDERING_MARKERS):
        return None

    matches: list[tuple[int, int, CandidateAgentV2]] = []
    for index, agent in enumerate(candidates):
        position = _first_match_position(lowered, agent)
        if position is not None:
            matches.append((position, index, agent))

    ordered_agents = [agent for _, _, agent in sorted(matches, key=lambda item: (item[0], item[1]))]
    if len(ordered_agents) < 2:
        return None

    steps: list[PlanStep] = []
    previous_step_id: str | None = None
    for index, agent in enumerate(ordered_agents, start=1):
        step_id = f"step_{index}_{_safe_step_suffix(agent.agent_id)}"
        steps.append(
            PlanStep(
                step_id=step_id,
                agent_id=agent.agent_id,
                description=f"{agent.name}: {agent.description}",
                depends_on=[previous_step_id] if previous_step_id else [],
            )
        )
        previous_step_id = step_id

    return Plan(
        plan_id=f"plan_{uuid4().hex}",
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=session_id,
        steps=steps,
    )


def _first_match_position(text: str, agent: CandidateAgentV2) -> int | None:
    positions = [position for token in _agent_tokens(agent) if (position := text.find(token)) >= 0]
    if not positions:
        return None
    return min(positions)


def _agent_tokens(agent: CandidateAgentV2) -> list[str]:
    raw_tokens = [
        agent.agent_id,
        agent.name,
        agent.description,
        *agent.capabilities,
        *agent.trigger.keywords,
        *agent.trigger.positive_examples,
    ]
    tokens: list[str] = []
    for raw in raw_tokens:
        value = str(raw).strip().lower().replace("_", " ")
        if len(value) >= 2:
            tokens.append(value)
        for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", value):
            if len(token) >= 3:
                tokens.append(token)
    return sorted(set(tokens), key=len, reverse=True)


def _safe_step_suffix(agent_id: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_]+", "_", agent_id).strip("_") or "agent"
