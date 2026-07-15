import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
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
        self._inflight: dict[str, asyncio.Task] = {}
        self._completed: dict[str, dict[str, Any]] = {}

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
        execution_key = invocation.context.get("plan_execution_idempotency_key")
        if isinstance(execution_key, str) and execution_key:
            result = await self._invoke_idempotent(execution_key, func, invocation)
        else:
            result = await self._call(func, invocation)
        return AgentInvocationResult(
            run_id=invocation.run_id,
            agent_id=definition.agent_id,
            status=str(result.get("status", "completed")),
            message=str(result.get("message", "")),
            output=result.get("output", result),
            usage=result.get("usage", {}),
        )

    async def _invoke_idempotent(self, execution_key: str, func, invocation) -> dict[str, Any]:
        completed = self._completed.get(execution_key)
        if completed is not None:
            return deepcopy(completed)
        task = self._inflight.get(execution_key)
        if task is None:
            task = asyncio.create_task(self._call(func, invocation))
            self._inflight[execution_key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(execution_key, None)
        self._completed[execution_key] = deepcopy(result)
        while len(self._completed) > 1000:
            self._completed.pop(next(iter(self._completed)))
        return deepcopy(result)

    async def _call(self, func, invocation) -> dict[str, Any]:
        if asyncio.iscoroutinefunction(func):
            result = func(invocation)
        else:
            result = await asyncio.to_thread(func, invocation)
        if isawaitable(result):
            result = await result
        return result
