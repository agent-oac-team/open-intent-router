from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

from app.core.errors import InvocationError
from app.schemas.agents import AgentDefinition
from app.schemas.invocation import AgentInvocation, AgentInvocationResult

LocalFunction = Callable[[AgentInvocation], dict[str, Any] | Awaitable[dict[str, Any]]]


class LocalFunctionRegistry:
    def __init__(self) -> None:
        self._functions: dict[str, LocalFunction] = {}

    def register(self, name: str, func: LocalFunction) -> None:
        self._functions[name] = func

    def get(self, name: str) -> LocalFunction | None:
        return self._functions.get(name)


class LocalFunctionInvoker:
    def __init__(self, registry: LocalFunctionRegistry | None = None) -> None:
        self.registry = registry or LocalFunctionRegistry()

    async def invoke(
        self,
        definition: AgentDefinition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        function_name = definition.invocation.config.get("function")
        if not function_name:
            raise InvocationError("local_function Agent requires invocation.config.function")
        func = self.registry.get(str(function_name))
        if func is None:
            raise InvocationError(f"Local function is not registered: {function_name}")
        result = func(invocation)
        if isawaitable(result):
            result = await result
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status=str(result.get("status", "completed")),
            message=str(result.get("message", "")),
            output=result.get("output", result),
            usage=result.get("usage", {}),
        )
