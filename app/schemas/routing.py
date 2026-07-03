from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas.agents import CandidateAgent
from app.schemas.common import (
    ArtifactRef,
    ContextRelation,
    ErrorDetail,
    ExecutionPolicy,
    JsonDict,
    MessageSource,
    RouteAction,
    RouteStatus,
    StrictBaseModel,
    UserContext,
    normalize_artifact_refs,
)
from app.schemas.context import ContextBudget
from app.schemas.plans import NextAction, Plan


class InputPayload(StrictBaseModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)
    attachments: list[JsonDict] = Field(default_factory=list)


class CurrentAgentContext(StrictBaseModel):
    agent_id: str = Field(min_length=1)
    run_id: str | None = None
    agent_session_id: str | None = None


class RouteRequest(StrictBaseModel):
    request_id: str | None = None
    session_id: str = Field(min_length=1)
    source: MessageSource = "host_chat"
    user: UserContext
    input: InputPayload
    current_agent: CurrentAgentContext | None = None
    event_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    context_budget: ContextBudget | None = None
    frontend_context: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_request_context(self) -> "RouteRequest":
        if self.source == "agent_chat" and self.current_agent is None:
            raise ValueError("current_agent is required when source=agent_chat")
        if self.source == "agent_event" and not self.event_id:
            raise ValueError("event_id is required when source=agent_event")
        if self.source == "plan_control" and not self.plan_id:
            raise ValueError("plan_id is required when source=plan_control")
        return self


class RouteDecision(StrictBaseModel):
    status: RouteStatus = "ok"
    action: RouteAction
    target_agent_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str = ""
    message: str = ""

    @model_validator(mode="after")
    def validate_target(self) -> "RouteDecision":
        if self.action in {"open_agent", "continue_agent"} and not self.target_agent_id:
            raise ValueError(f"target_agent_id is required when action={self.action}")
        if self.action in {"reply", "clarify", "unsupported", "silent", "exit_agent"}:
            if self.target_agent_id is not None and self.action != "exit_agent":
                raise ValueError(f"target_agent_id must be null when action={self.action}")
        return self


class RouteContext(StrictBaseModel):
    relation: ContextRelation = "new_task"
    current_agent_id: str | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    candidate_agent_ids: list[str] = Field(default_factory=list)
    intent_hint: str | None = None
    evidence: list[JsonDict] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_refs(cls, data: Any) -> Any:
        if isinstance(data, dict) and "artifact_refs" in data:
            data = dict(data)
            data["artifact_refs"] = normalize_artifact_refs(data.get("artifact_refs") or [])
        return data


class InvocationPreview(StrictBaseModel):
    mode: Literal["deferred", "invoke", "ui_handoff"] = "deferred"
    agent_id: str
    input: JsonDict = Field(default_factory=dict)
    metadata: JsonDict = Field(default_factory=dict)


class RouteResponse(StrictBaseModel):
    request_id: str
    session_id: str
    assistant_message: str | None = None
    decision: RouteDecision
    context: RouteContext
    execution_policy: ExecutionPolicy | None = None
    next_action: NextAction | None = None
    plan: Plan | None = None
    invocation: InvocationPreview | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_action_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            decision = data.get("decision")
            context = data.get("context")
            if isinstance(decision, dict) and decision.get("action") == "switch_agent":
                decision["action"] = "open_agent"
                if isinstance(context, dict):
                    context["relation"] = "switch_agent"
        return data

    @model_validator(mode="after")
    def validate_route_response(self) -> "RouteResponse":
        if self.decision.action == "show_plan" and self.plan is None:
            raise ValueError("plan is required when decision.action=show_plan")
        if self.decision.action in {"open_agent", "continue_agent"}:
            if self.decision.target_agent_id not in self.context.candidate_agent_ids:
                raise ValueError("target_agent_id must be in candidate_agent_ids")
        if self.decision.action == "continue_agent":
            if self.context.current_agent_id != self.decision.target_agent_id:
                raise ValueError("continue_agent target must match current_agent_id")
        return self


class LLMRouteInput(StrictBaseModel):
    request: RouteRequest
    candidates: list[CandidateAgent]
    context: RouteContext


class RouteAndExecuteResponse(StrictBaseModel):
    route: RouteResponse
    results: list[JsonDict] = Field(default_factory=list)
    next_action: NextAction | None = None
