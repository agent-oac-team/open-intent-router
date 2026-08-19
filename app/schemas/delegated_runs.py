from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import JsonDict, StrictBaseModel
from app.schemas.logs import ExecutionBindingSnapshot

DELEGATED_RUN_MAINTENANCE_EVENT_PREFIX = "event_timeout_"


class DelegatedRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED, self.TIMED_OUT}


class DelegatedRunStartCommand(StrictBaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    plan_id: str | None = Field(default=None, max_length=128)
    step_id: str | None = Field(default=None, max_length=128)
    deadline_at: datetime
    input: JsonDict = Field(default_factory=dict)
    metadata: JsonDict = Field(default_factory=dict)
    agent_revision: int | None = Field(default=None, ge=0)
    handling_kind: Literal["invocation", "external_execution", "ui_handoff"] | None = None
    binding_snapshot: ExecutionBindingSnapshot | None = None


class DelegatedRunEventCommand(StrictBaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    plan_id: str | None = Field(default=None, max_length=128)
    step_id: str | None = Field(default=None, max_length=128)
    expected_state_version: int = Field(ge=1)
    occurred_at: datetime


class DelegatedRunProgressCommand(DelegatedRunEventCommand):
    event_type: Literal["agent_started", "agent_progress", "agent_clarify"] = "agent_progress"
    sequence: int = Field(ge=1)
    status: str = Field(default="running", pattern=r"^(running|blocked)$")
    payload: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_event_status(self) -> "DelegatedRunProgressCommand":
        expected = "blocked" if self.event_type == "agent_clarify" else "running"
        if self.status != expected:
            raise ValueError(f"status={self.status} conflicts with event_type={self.event_type}")
        return self


class DelegatedRunCompleteCommand(DelegatedRunEventCommand):
    result_id: str = Field(min_length=1, max_length=128)
    response_text: str = Field(default="", max_length=20000)
    output: JsonDict | None = None
    artifact_refs: list[JsonDict] = Field(default_factory=list, max_length=50)


class DelegatedRunFailCommand(DelegatedRunEventCommand):
    error: JsonDict


class DelegatedRunCancelCommand(DelegatedRunEventCommand):
    reason: str = Field(min_length=1, max_length=500)


class DelegatedRunTimeoutCommand(DelegatedRunEventCommand):
    deadline_at: datetime
    reason: str = Field(default="deadline_exceeded", min_length=1, max_length=500)


class DelegatedRunReference(StrictBaseModel):
    run_id: str
    turn_id: str
    tenant_id: str
    user_id: str
    agent_id: str
    plan_id: str | None = None
    step_id: str | None = None
    status: DelegatedRunStatus
    state_version: int = Field(ge=1)
    # The Ticket callback cursor must advance with the canonical Run. Keeping
    # it here prevents a retried External Execution response from resetting to 0.
    event_sequence: int = Field(default=0, ge=0)
    deadline_at: datetime


class DelegatedRunExisting(StrictBaseModel):
    """Canonical immutable execution facts for one idempotent Delegated Run."""

    run: DelegatedRunReference
    agent_revision: int | None = Field(default=None, ge=0)
    handling_kind: Literal["invocation", "external_execution", "ui_handoff"] | None = None
    binding_snapshot: ExecutionBindingSnapshot | None = None


class DelegatedRunCommandResult(StrictBaseModel):
    run: DelegatedRunReference
    accepted: bool = True
    duplicate: bool = False
    result_id: str | None = None
    turn_id: str


class DelegatedRunOrphanQuery(StrictBaseModel):
    now: datetime
    stale_before: datetime
    tenant_id: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=100, ge=1, le=1000)


class DelegatedRunOverdueQuery(StrictBaseModel):
    now: datetime
    tenant_id: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=100, ge=1, le=1000)


class DelegatedRunOrphanResponse(StrictBaseModel):
    runs: list[DelegatedRunReference] = Field(default_factory=list)
