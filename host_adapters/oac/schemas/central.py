from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import JsonDict, StrictBaseModel

LegacySource = Literal["central_chat", "agent_chat", "agent_event", "plan_control"]
LegacyRouteAction = Literal[
    "reply",
    "clarify",
    "open_agent",
    "continue_agent",
    "exit_agent",
    "show_plan",
    "unsupported",
    "silent",
]


class LegacyChatMessage(StrictBaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)
    created_at: datetime | None = None


class CentralRouteRequest(StrictBaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    user_tags: list[str] = Field(default_factory=list)
    source: LegacySource
    user_query: str = ""
    central_chat_history: list[LegacyChatMessage] = Field(default_factory=list)
    agent_chat_history: list[LegacyChatMessage] = Field(default_factory=list)
    current_agent_id: str | None = None
    current_agent_session_id: str | None = None
    frontend_context: JsonDict = Field(default_factory=dict)
    event_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    plan_action: Literal["confirm", "continue"] | None = None


class LegacyRoute(StrictBaseModel):
    status: Literal["ok", "clarify", "unsupported", "error"]
    action: LegacyRouteAction
    agent_id: str | None = None
    message: str = ""


class LegacyRouteContext(StrictBaseModel):
    source: LegacySource
    current_agent_id: str | None = None
    relation: Literal[
        "new_task",
        "continue_current",
        "switch_agent",
        "exit_agent",
        "multi_task",
        "unsupported",
    ]
    artifact_refs: list[str] = Field(default_factory=list)


class LegacyPlanStep(StrictBaseModel):
    step_id: str
    agent_id: str
    status: Literal["pending", "running", "blocked", "completed", "failed"]
    description: str
    runtime_status: str | None = None


class LegacyPlan(StrictBaseModel):
    plan_id: str
    current_step: str
    steps: list[LegacyPlanStep]


class CentralRouteResponse(StrictBaseModel):
    request_id: str
    session_id: str
    route: LegacyRoute
    context: LegacyRouteContext
    plan: LegacyPlan | None = None
    execution_ticket: str | None = None


class NavigationEventRequest(StrictBaseModel):
    event_type: Literal["navigation"] = "navigation"
    session_id: str
    user_id: str
    from_path: str = Field(alias="from")
    to: str
    reason: str
    current_agent_id: str | None = None
    current_agent_session_id: str | None = None
    created_at: datetime | None = None


class AcceptedResponse(StrictBaseModel):
    accepted: bool


class AgentEventRequest(StrictBaseModel):
    event_id: str
    session_id: str
    agent_id: str
    status: Literal["pending", "running", "completed", "failed", "blocked", "clarify"]
    event_type: Literal["agent_result", "agent_progress", "agent_error", "agent_clarify"] = (
        "agent_result"
    )
    agent_session_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    result_ref: str | None = None
    output: JsonDict | str | None = None
    message: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    execution_ticket: str | None = None


class AgentEventCompatResponse(StrictBaseModel):
    event_id: str
    session_id: str
    accepted: bool
    duplicate: bool = False
    route_required: bool = True


class PlanConfirmResponse(StrictBaseModel):
    plan_id: str
    session_id: str
    status: str
    current_step_id: str | None = None
    current_step: JsonDict | None = None


class CompatErrorResponse(StrictBaseModel):
    code: str
    message: str
    details: JsonDict = Field(default_factory=dict)
