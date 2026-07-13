from typing import Protocol

from app.schemas.agents import AgentDefinition
from app.schemas.events import AgentEvent, ConversationEvent
from app.schemas.logs import AgentResult, AgentRun, RouteLog
from app.schemas.plans import Plan
from app.schemas.sessions import ChatMessage


class AgentDefinitionRepository(Protocol):
    async def list(self, *, enabled_only: bool = False) -> list[AgentDefinition]: ...

    async def get(self, agent_id: str) -> AgentDefinition | None: ...

    async def upsert(self, definition: AgentDefinition) -> AgentDefinition: ...

    async def set_enabled(self, agent_id: str, enabled: bool) -> AgentDefinition | None: ...

    async def delete(self, agent_id: str) -> bool: ...


class MessageRepository(Protocol):
    async def add(self, message: ChatMessage) -> ChatMessage: ...

    async def list_by_session(
        self,
        session_id: str,
        *,
        source: str | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[ChatMessage]: ...


class EventRepository(Protocol):
    async def add_conversation_event(self, event: ConversationEvent) -> ConversationEvent: ...

    async def add_agent_event(self, event: AgentEvent) -> tuple[AgentEvent, bool]: ...

    async def get_event(self, event_id: str) -> AgentEvent | None: ...

    async def list_recent_events(self, session_id: str, *, limit: int = 10) -> list[AgentEvent]: ...


class RunRepository(Protocol):
    async def add_run(self, run: AgentRun) -> AgentRun: ...

    async def update_run(self, run: AgentRun) -> AgentRun: ...

    async def get_run(self, run_id: str) -> AgentRun | None: ...


class ResultRepository(Protocol):
    async def add_result(self, result: AgentResult) -> AgentResult: ...

    async def list_recent(self, session_id: str, *, limit: int = 5) -> list[AgentResult]: ...


class PlanRepository(Protocol):
    async def save(self, plan: Plan) -> Plan: ...

    async def get(self, plan_id: str) -> Plan | None: ...

    async def get_active_by_session(self, session_id: str) -> Plan | None: ...


class RouteLogRepository(Protocol):
    async def add(self, log: RouteLog) -> RouteLog: ...
