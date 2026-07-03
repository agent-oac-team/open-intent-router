from datetime import datetime

from pydantic import Field, model_validator

from app.schemas.common import JsonDict, MessageSource, ParticipantRole, StrictBaseModel


class ChatMessage(StrictBaseModel):
    message_id: str
    session_id: str
    user_id: str | None = None
    source: MessageSource
    role: ParticipantRole
    content: str = Field(min_length=1)
    agent_id: str | None = None
    agent_session_id: str | None = None
    request_id: str | None = None
    event_id: str | None = None
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime | None = None


class AppendChatMessageRequest(StrictBaseModel):
    source: MessageSource
    role: ParticipantRole
    content: str = Field(min_length=1)
    user_id: str | None = None
    agent_id: str | None = None
    agent_session_id: str | None = None
    request_id: str | None = None
    event_id: str | None = None
    metadata: JsonDict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_agent_chat(self) -> "AppendChatMessageRequest":
        if self.source == "agent_chat" and not self.agent_id:
            raise ValueError("agent_id is required when source=agent_chat")
        return self


class ChatHistoryResponse(StrictBaseModel):
    session_id: str
    messages: list[ChatMessage]
