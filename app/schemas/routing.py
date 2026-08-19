from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, PrivateAttr, model_validator

from app.schemas.agents import AgentDefinition, AgentDefinitionV2, CandidateAgent
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
from app.schemas.context import ContextBudget, ContextProjection
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
    _selected_definitions: dict[str, AgentDefinition | AgentDefinitionV2] = PrivateAttr(
        default_factory=dict
    )
    _selected_bindings: dict[str, object] = PrivateAttr(default_factory=dict)
    _routed_source_request: RouteRequest | None = PrivateAttr(default=None)
    _routed_source_request_id: str | None = PrivateAttr(default=None)
    _routed_user: UserContext | None = PrivateAttr(default=None)
    _routed_session_id: str | None = PrivateAttr(default=None)
    _routed_response_request_id: str | None = PrivateAttr(default=None)
    _routed_invocation: InvocationPreview | None = PrivateAttr(default=None)
    _routed_execution_bound: bool = PrivateAttr(default=False)

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

    def bind_selected_definitions(
        self,
        definitions: (
            list[AgentDefinition | AgentDefinitionV2]
            | dict[str, AgentDefinition | AgentDefinitionV2]
        ),
    ) -> "RouteResponse":
        self._ensure_execution_unbound()
        values = definitions.values() if isinstance(definitions, dict) else definitions
        self._selected_definitions = {item.agent_id: item for item in values}
        return self

    def bind_selected_bindings(self, bindings: Mapping[str, object]) -> "RouteResponse":
        """Retain trusted, request-scoped Binding selections outside the wire payload."""

        self._ensure_execution_unbound()
        self._selected_bindings = dict(bindings)
        return self

    def bind_routed_execution(self, request: RouteRequest) -> "RouteResponse":
        """Freeze the trusted Route capability outside of the mutable wire response."""

        self._ensure_execution_unbound()
        self._routed_source_request = request
        self._routed_source_request_id = request.request_id
        self._routed_user = request.user.model_copy(deep=True)
        self._routed_session_id = request.session_id
        self._routed_response_request_id = self.request_id
        self._routed_invocation = (
            self.invocation.model_copy(deep=True) if self.invocation is not None else None
        )
        self._routed_execution_bound = True
        return self

    def _ensure_execution_unbound(self) -> None:
        if self._routed_execution_bound:
            raise RuntimeError("Routed execution capability is immutable")

    @property
    def selected_definitions(self) -> dict[str, AgentDefinition | AgentDefinitionV2]:
        return dict(self._selected_definitions)

    def selected_definition(self, agent_id: str) -> AgentDefinition | AgentDefinitionV2 | None:
        return self._selected_definitions.get(agent_id)

    def selected_binding(self, agent_id: str) -> object | None:
        return self._selected_bindings.get(agent_id)

    def has_trusted_invocation_for(self, request: RouteRequest) -> bool:
        """Whether this mutable response still represents its routed Invocation."""

        trusted_invocation = self._routed_invocation
        return (
            self._routed_execution_bound
            and trusted_invocation is not None
            and self.invocation == trusted_invocation
            and self.decision.action in {"open_agent", "continue_agent"}
            and self.decision.target_agent_id == trusted_invocation.agent_id
            and self._routed_source_request is request
            and self._routed_source_request_id == request.request_id
            and self._routed_user == request.user
            and self._routed_session_id == request.session_id == self.session_id
            and self._routed_response_request_id == self.request_id
        )

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
    projection: ContextProjection | None = None


class RouteAndExecuteResponse(StrictBaseModel):
    route: RouteResponse
    results: list[JsonDict] = Field(default_factory=list)
    next_action: NextAction | None = None
