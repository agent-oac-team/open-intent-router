from app.schemas.agents import LegacyAgentDefinition
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class MockAgentInvoker:
    async def invoke(
        self,
        definition: LegacyAgentDefinition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        output = definition.invocation.config.get("response", {})
        message = definition.invocation.config.get("message", f"{definition.name} completed.")
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status="completed",
            message=message,
            output=output,
            usage={"mock": True},
        )
