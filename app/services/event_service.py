from app.schemas.events import AgentEvent, AgentEventResponse, ConversationEvent


class EventService:
    def __init__(self, repository) -> None:
        self.repository = repository

    async def record_conversation_event(self, event: ConversationEvent) -> ConversationEvent:
        return await self.repository.add_conversation_event(event)

    async def record_agent_event(self, event: AgentEvent) -> AgentEventResponse:
        saved, duplicate = await self.repository.add_agent_event(event)
        return AgentEventResponse(event_id=saved.event_id, accepted=True, duplicate=duplicate)

    async def get_event(self, event_id: str) -> AgentEvent | None:
        return await self.repository.get_event(event_id)

    async def list_recent_events(self, session_id: str, *, limit: int = 10) -> list[AgentEvent]:
        return await self.repository.list_recent_events(session_id, limit=max(0, limit))
