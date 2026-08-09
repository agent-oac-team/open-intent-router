from app.core.errors import InvocationError
from app.invokers.base import AgentInvoker
from app.schemas.common import AgentType


class AgentInvokerRegistry:
    def __init__(self) -> None:
        self._invokers: dict[AgentType, AgentInvoker] = {}

    def register(self, agent_type: AgentType, invoker: AgentInvoker) -> None:
        self._invokers[agent_type] = invoker

    def get(self, agent_type: AgentType) -> AgentInvoker:
        invoker = self._invokers.get(agent_type)
        if invoker is None:
            raise InvocationError(f"No invoker registered for Agent type: {agent_type}")
        return invoker

    def has(self, agent_type: AgentType) -> bool:
        return agent_type in self._invokers
