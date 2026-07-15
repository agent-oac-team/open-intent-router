from datetime import datetime

from pydantic import Field

from app.schemas.common import AgentEventType, JsonDict, MessageSource, StrictBaseModel


class AgentEvent(StrictBaseModel):
    event_id: str = Field(min_length=1)
    run_id: str | None = None
    request_id: str | None = None
    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    user_id: str | None = None
    tenant_id: str | None = None
    agent_session_id: str | None = None
    event_type: AgentEventType
    status: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    payload: JsonDict = Field(default_factory=dict)
    created_at: datetime | None = None


class ConversationEvent(StrictBaseModel):
    event_id: str
    session_id: str
    request_id: str | None = None
    user_id: str | None = None
    event_type: str
    source: MessageSource | None = None
    agent_id: str | None = None
    payload: JsonDict = Field(default_factory=dict)
    created_at: datetime | None = None


class AgentEventResponse(StrictBaseModel):
    event_id: str
    accepted: bool = True
    duplicate: bool = False
    message: str = ""
