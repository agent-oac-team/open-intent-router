from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

JsonDict = dict[str, Any]
JsonList = list[Any]

AgentType = Literal[
    "http",
    "local_function",
    "mock",
    "workflow",
    "provider_platform",
    "ui_handoff",
]
RouteStatus = Literal["ok", "clarify", "unsupported", "error"]
RouteAction = Literal[
    "reply",
    "clarify",
    "open_agent",
    "continue_agent",
    "exit_agent",
    "show_plan",
    "unsupported",
    "silent",
]
ContextRelation = Literal[
    "new_task",
    "continue_current",
    "switch_agent",
    "exit_agent",
    "multi_task",
    "unsupported",
]
MessageSource = Literal["host_chat", "agent_chat", "agent_event", "plan_control", "system"]
ParticipantRole = Literal["user", "assistant", "system", "agent", "router"]
PlanStatus = Literal["pending", "running", "blocked", "completed", "failed", "cancelled"]
PlanStepStatus = Literal["pending", "running", "blocked", "completed", "failed", "cancelled"]
AgentRunStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "blocked",
    "clarify",
    "invalid_output",
]
ExecutionPolicy = Literal[
    "return_plan_only",
    "require_confirmation",
    "auto_execute",
    "host_managed",
]
NextActionType = Literal[
    "confirm_plan",
    "open_ui",
    "collect_input",
    "wait_for_agent_event",
    "none",
]
AgentEventType = Literal[
    "agent_started",
    "agent_progress",
    "agent_result",
    "agent_error",
    "agent_clarify",
    "agent_cancelled",
]


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ErrorDetail(StrictBaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: JsonDict = Field(default_factory=dict)


class UserContext(StrictBaseModel):
    id: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    attributes: JsonDict = Field(default_factory=dict)

    @property
    def tenant_id(self) -> str | None:
        value = self.attributes.get("tenant_id") or self.attributes.get("tenant")
        return str(value) if value is not None else None


class ArtifactRef(StrictBaseModel):
    artifact_id: str = Field(min_length=1)
    type: str = "unknown"
    uri: str = Field(min_length=1)
    title: str | None = None
    metadata: JsonDict = Field(default_factory=dict)


def normalize_artifact_refs(raw_refs: list[str | ArtifactRef | JsonDict]) -> list[ArtifactRef]:
    refs: list[ArtifactRef] = []
    for item in raw_refs:
        if isinstance(item, ArtifactRef):
            refs.append(item)
        elif isinstance(item, str):
            refs.append(ArtifactRef(artifact_id=item, uri=item))
        else:
            refs.append(ArtifactRef.model_validate(item))
    return refs


class TimeStampedModel(StrictBaseModel):
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SchemaContract(StrictBaseModel):
    type: str = "object"
    required: list[str] = Field(default_factory=list)
    properties: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_object_schema(self) -> "SchemaContract":
        if self.type != "object":
            raise ValueError("Only object JSON schemas are supported in the MVP")
        return self
