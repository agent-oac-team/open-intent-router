from app.schemas.agents import AgentDefinition
from app.schemas.events import AgentEvent, ConversationEvent
from app.schemas.logs import AgentResult, AgentRun, RouteLog
from app.schemas.plans import Plan
from app.schemas.sessions import ChatMessage


class MemoryAgentDefinitionRepository:
    def __init__(self, agents: list[AgentDefinition] | None = None) -> None:
        self.agents = {agent.agent_id: agent for agent in agents or []}

    async def list(self, *, enabled_only: bool = False) -> list[AgentDefinition]:
        values = list(self.agents.values())
        if enabled_only:
            values = [agent for agent in values if agent.enabled]
        return sorted(values, key=lambda item: (-item.priority, item.agent_id))

    async def get(self, agent_id: str) -> AgentDefinition | None:
        return self.agents.get(agent_id)

    async def upsert(self, definition: AgentDefinition) -> AgentDefinition:
        self.agents[definition.agent_id] = definition
        return definition

    async def set_enabled(self, agent_id: str, enabled: bool) -> AgentDefinition | None:
        agent = self.agents.get(agent_id)
        if agent is None:
            return None
        updated = agent.model_copy(update={"enabled": enabled})
        self.agents[agent_id] = updated
        return updated

    async def delete(self, agent_id: str) -> bool:
        return self.agents.pop(agent_id, None) is not None


class MemoryMessageRepository:
    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []

    async def add(self, message: ChatMessage) -> ChatMessage:
        self.messages.append(message)
        return message

    async def list_by_session(
        self,
        session_id: str,
        *,
        source: str | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[ChatMessage]:
        items = [item for item in self.messages if item.session_id == session_id]
        if source:
            items = [item for item in items if item.source == source]
        if agent_id:
            items = [item for item in items if item.agent_id == agent_id]
        return items[-limit:]


class MemoryEventRepository:
    def __init__(self) -> None:
        self.conversation_events: list[ConversationEvent] = []
        self.agent_events: dict[str, AgentEvent] = {}

    async def add_conversation_event(self, event: ConversationEvent) -> ConversationEvent:
        self.conversation_events.append(event)
        return event

    async def add_agent_event(self, event: AgentEvent) -> tuple[AgentEvent, bool]:
        existing = self.agent_events.get(event.event_id)
        if existing:
            return existing, True
        self.agent_events[event.event_id] = event
        return event, False

    async def get_event(self, event_id: str) -> AgentEvent | None:
        return self.agent_events.get(event_id)

    async def list_recent_events(self, session_id: str, *, limit: int = 10) -> list[AgentEvent]:
        bounded_limit = max(0, limit)
        items = [item for item in self.agent_events.values() if item.session_id == session_id]
        return list(reversed(items[-bounded_limit:])) if bounded_limit else []


class MemoryRunRepository:
    def __init__(self) -> None:
        self.runs: dict[str, AgentRun] = {}

    async def add_run(self, run: AgentRun) -> AgentRun:
        self.runs[run.run_id] = run
        return run

    async def update_run(self, run: AgentRun) -> AgentRun:
        self.runs[run.run_id] = run
        return run

    async def get_run(self, run_id: str) -> AgentRun | None:
        return self.runs.get(run_id)


class MemoryResultRepository:
    def __init__(self) -> None:
        self.results: list[AgentResult] = []

    async def add_result(self, result: AgentResult) -> AgentResult:
        self.results.append(result)
        return result

    async def list_recent(self, session_id: str, *, limit: int = 5) -> list[AgentResult]:
        items = [item for item in self.results if item.session_id == session_id]
        return list(reversed(items[-limit:]))


class MemoryPlanRepository:
    def __init__(self) -> None:
        self.plans: dict[str, Plan] = {}

    async def save(self, plan: Plan) -> Plan:
        self.plans[plan.plan_id] = plan
        return plan

    async def get(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)

    async def get_active_by_session(self, session_id: str) -> Plan | None:
        active = [
            plan
            for plan in self.plans.values()
            if plan.session_id == session_id and plan.status in {"pending", "running", "blocked"}
        ]
        return active[-1] if active else None


class MemoryRouteLogRepository:
    def __init__(self) -> None:
        self.logs: list[RouteLog] = []

    async def add(self, log: RouteLog) -> RouteLog:
        self.logs.append(log)
        return log
