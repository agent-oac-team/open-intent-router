from uuid import uuid4

from app.schemas.common import MessageSource
from app.schemas.sessions import AppendChatMessageRequest, ChatMessage


class ChatHistoryService:
    def __init__(self, repository, *, host_limit: int, agent_limit: int) -> None:
        self.repository = repository
        self.host_limit = host_limit
        self.agent_limit = agent_limit

    async def record(self, message: ChatMessage) -> ChatMessage:
        return await self.repository.add(message)

    async def append_message(
        self,
        *,
        session_id: str,
        payload: AppendChatMessageRequest,
    ) -> ChatMessage:
        return await self.record(
            ChatMessage(
                message_id=f"msg_{uuid4().hex}",
                session_id=session_id,
                user_id=payload.user_id,
                tenant_id=payload.tenant_id,
                source=payload.source,
                role=payload.role,
                content=payload.content,
                agent_id=payload.agent_id,
                agent_session_id=payload.agent_session_id,
                request_id=payload.request_id,
                event_id=payload.event_id,
                metadata=payload.metadata,
            )
        )

    async def append_owned_message(
        self,
        *,
        session_id: str,
        payload: AppendChatMessageRequest,
        tenant_id: str,
        user_id: str,
    ) -> ChatMessage:
        if payload.user_id not in {None, user_id} or payload.tenant_id not in {
            None,
            tenant_id,
        }:
            raise ValueError("Session message owner does not match Principal")
        return await self.append_message(
            session_id=session_id,
            payload=payload.model_copy(update={"tenant_id": tenant_id, "user_id": user_id}),
        )

    async def record_user_input(
        self,
        *,
        session_id: str,
        user_id: str,
        tenant_id: str,
        content: str,
        source: MessageSource = "host_chat",
        request_id: str | None = None,
        event_id: str | None = None,
        agent_id: str | None = None,
        agent_session_id: str | None = None,
    ) -> ChatMessage:
        return await self.record(
            ChatMessage(
                message_id=f"msg_{uuid4().hex}",
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                source=source,
                role="user",
                content=content,
                request_id=request_id,
                event_id=event_id,
                agent_id=agent_id,
                agent_session_id=agent_session_id,
            )
        )

    async def get_host_history(
        self, session_id: str, *, tenant_id: str, user_id: str
    ) -> list[ChatMessage]:
        return await self.repository.list_by_session(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            source="host_chat",
            limit=self.host_limit,
        )

    async def get_agent_history(
        self, session_id: str, agent_id: str, *, tenant_id: str, user_id: str
    ) -> list[ChatMessage]:
        return await self.repository.list_by_session(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            source="agent_chat",
            agent_id=agent_id,
            limit=self.agent_limit,
        )
