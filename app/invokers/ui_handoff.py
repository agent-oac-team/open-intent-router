from app.schemas.agents import LegacyAgentDefinition
from app.schemas.invocation import AgentInvocation, AgentInvocationResult


class UiHandoffInvoker:
    async def invoke(
        self,
        definition: LegacyAgentDefinition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        output = {
            "mode": definition.ui_handoff.mode,
            "route": definition.ui_handoff.route,
            "params": definition.ui_handoff.params,
            "input": invocation.input,
        }
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status="completed",
            message="Host application handoff prepared.",
            output=output,
        )
