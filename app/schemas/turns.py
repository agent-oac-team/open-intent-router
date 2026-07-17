from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import JsonDict, StrictBaseModel

TurnIdentifier = str
TurnResponseKind = Literal["reply", "clarify", "unsupported", "silent", "agent_result", "error"]


class TurnStatus(StrEnum):
    PENDING = "pending"
    ROUTING = "routing"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED, self.TIMED_OUT}


class TurnResultReferences(StrictBaseModel):
    route_decision_id: TurnIdentifier | None = None
    run_ids: list[TurnIdentifier] = Field(default_factory=list, max_length=50)
    result_ids: list[TurnIdentifier] = Field(default_factory=list, max_length=50)
    plan_id: TurnIdentifier | None = None
    event_ids: list[TurnIdentifier] = Field(default_factory=list, max_length=100)


class TurnUserInput(StrictBaseModel):
    text: str = Field(max_length=20000)
    metadata: JsonDict = Field(default_factory=dict)


class TurnSemanticResponse(StrictBaseModel):
    kind: TurnResponseKind
    text: str = Field(default="", max_length=20000)
    output: JsonDict | None = None
    error: JsonDict | None = None


class CanonicalTurn(StrictBaseModel):
    turn_id: TurnIdentifier
    tenant_id: TurnIdentifier
    user_id: TurnIdentifier
    session_id: TurnIdentifier
    request_id: TurnIdentifier
    source: str = Field(min_length=1, max_length=64)
    status: TurnStatus = TurnStatus.PENDING
    state_version: int = Field(default=1, ge=1)
    user_input: TurnUserInput
    references: TurnResultReferences = Field(default_factory=TurnResultReferences)
    final_response: TurnSemanticResponse | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "CanonicalTurn":
        if self.status == TurnStatus.COMPLETED and self.final_response is None:
            raise ValueError("completed turn requires a final semantic response")
        if self.status.is_terminal != (self.completed_at is not None):
            raise ValueError("terminal turn and completed_at must be set together")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must not precede created_at")
        return self


class TurnCapsule(StrictBaseModel):
    turn_id: TurnIdentifier
    tenant_id: TurnIdentifier
    user_id: TurnIdentifier
    session_id: TurnIdentifier
    request_id: TurnIdentifier
    source: str = Field(min_length=1, max_length=64)
    state_version: int = Field(ge=1)
    user_input: TurnUserInput
    final_response: TurnSemanticResponse
    references: TurnResultReferences = Field(default_factory=TurnResultReferences)
    completed_at: datetime


class TurnOutboxEvent(StrictBaseModel):
    outbox_id: TurnIdentifier
    turn_id: TurnIdentifier
    event_type: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=512)
    payload: JsonDict = Field(default_factory=dict)
    status: Literal["pending", "claimed", "retry", "completed", "dead_letter"] = "pending"
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    available_at: datetime
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    published_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
