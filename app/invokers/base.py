from typing import Protocol

from app.schemas.agents import LegacyAgentDefinition
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class AgentInvoker(Protocol):
    async def invoke(
        self,
        definition: LegacyAgentDefinition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult: ...
