from typing import Protocol

from app.schemas.agents import AgentDefinition
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class AgentInvoker(Protocol):
    async def invoke(
        self,
        definition: AgentDefinition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult: ...
