import time
from uuid import uuid4

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema

from app.core.errors import InvocationError
from app.invokers.http import HttpAgentInvoker
from app.invokers.local_function import LocalFunctionInvoker, LocalFunctionRegistry
from app.invokers.mock import MockAgentInvoker
from app.invokers.registry import AgentInvokerRegistry
from app.invokers.ui_handoff import UiHandoffInvoker
from app.schemas.agent_context import KnowledgeContext, MemoryContext
from app.schemas.common import ErrorDetail
from app.schemas.invocation import AgentInvocation, AgentInvocationResult, InvokeRequest
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.routing import RouteRequest, RouteResponse
from app.services.registry_service import AgentRegistryService


class InvocationService:
    def __init__(
        self,
        registry: AgentRegistryService,
        run_repository,
        result_repository,
        invokers: AgentInvokerRegistry,
        agent_context_service=None,
    ) -> None:
        self.registry = registry
        self.run_repository = run_repository
        self.result_repository = result_repository
        self.invokers = invokers
        self.agent_context_service = agent_context_service

    async def invoke(self, request: InvokeRequest) -> AgentInvocationResult:
        definition = await self.registry.get_definition(request.agent_id)
        if definition is None:
            raise InvocationError(f"Agent not found: {request.agent_id}")
        run_id = f"run_{uuid4().hex}"
        invocation = AgentInvocation(
            run_id=run_id,
            request_id=request.request_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            user=request.user,
            input=request.input,
            context=request.context,
            memory_context=request.memory_context or MemoryContext(),
            knowledge_context=request.knowledge_context or KnowledgeContext(),
        )
        return await self._invoke_definition(definition, invocation)

    async def invoke_agent(
        self,
        *,
        agent_id: str,
        session_id: str,
        user,
        input: dict,
        context: dict | None = None,
        request_id: str | None = None,
    ) -> AgentInvocationResult:
        definition = await self.registry.get_definition(agent_id)
        if definition is None:
            raise InvocationError(f"Agent not found: {agent_id}")
        invocation = AgentInvocation(
            run_id=f"run_{uuid4().hex}",
            request_id=request_id,
            session_id=session_id,
            agent_id=agent_id,
            user=user,
            input=input,
            context=context or {},
        )
        return await self._invoke_definition(definition, invocation)

    async def invoke_from_route(
        self,
        route_request: RouteRequest,
        route_response: RouteResponse,
    ) -> AgentInvocationResult | None:
        preview = route_response.invocation
        if preview is None:
            return None
        definition = await self.registry.get_definition(preview.agent_id)
        if definition is None:
            raise InvocationError(f"Agent not found: {preview.agent_id}")
        invocation = AgentInvocation(
            run_id=f"run_{uuid4().hex}",
            request_id=route_response.request_id,
            session_id=route_response.session_id,
            agent_id=preview.agent_id,
            user=route_request.user,
            input=preview.input,
            context={"route_reason": route_response.decision.reason, **preview.metadata},
        )
        return await self._invoke_definition(definition, invocation)

    async def _invoke_definition(
        self,
        definition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        invocation = await self._with_agent_context(definition, invocation)
        started = time.perf_counter()
        run = AgentRun(
            run_id=invocation.run_id,
            request_id=invocation.request_id,
            session_id=invocation.session_id,
            agent_id=invocation.agent_id,
            status="running",
            invoker_type=definition.type,
            input=invocation.input,
        )
        await self.run_repository.add_run(run)
        invoker = self.invokers.get(definition.type)
        try:
            result = await invoker.invoke(definition, invocation)
            result = _validate_output(definition, result)
        except Exception as exc:
            if isinstance(exc, InvocationError):
                error = ErrorDetail(code=exc.code, message=exc.message, details=exc.details or {})
            else:
                error = ErrorDetail(code="invocation_failed", message=str(exc))
            result = AgentInvocationResult(
                run_id=invocation.run_id,
                agent_id=definition.agent_id,
                status="failed",
                message="Agent invocation failed.",
                error=error,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        result.usage.setdefault("latency_ms", latency_ms)
        await self.run_repository.update_run(
            run.model_copy(
                update={
                    "status": result.status,
                    "output": result.output,
                    "error": result.error.model_dump() if result.error else None,
                    "latency_ms": latency_ms,
                }
            )
        )
        await self.result_repository.add_result(
            AgentResult(
                result_id=f"result_{uuid4().hex}",
                run_id=invocation.run_id,
                session_id=invocation.session_id,
                agent_id=definition.agent_id,
                plan_id=_context_str(invocation.context, "plan_id"),
                step_id=_context_str(invocation.context, "step_id"),
                status=result.status,
                output=result.output,
                artifact_refs=[ref.model_dump() for ref in result.artifact_refs],
                error=result.error.model_dump() if result.error else None,
            )
        )
        return result

    async def _with_agent_context(
        self,
        definition,
        invocation: AgentInvocation,
    ) -> AgentInvocation:
        if not self.agent_context_service:
            return invocation
        input_values = dict(invocation.input or {})
        if "memory_context" not in input_values and _has_memory_context(invocation.memory_context):
            input_values["memory_context"] = invocation.memory_context.model_dump(mode="json")
        if "knowledge_context" not in input_values and _has_knowledge_context(
            invocation.knowledge_context
        ):
            input_values["knowledge_context"] = invocation.knowledge_context.model_dump(mode="json")
        query = _query_text(input_values)
        runtime = await self.agent_context_service.assemble(
            agent=definition,
            user=invocation.user,
            session_id=invocation.session_id,
            query=query,
            invocation_input=input_values,
            caller_type="agent",
            caller_id=definition.agent_id,
            purpose="agent_execution",
        )
        return invocation.model_copy(
            update={
                "input": input_values,
                "memory_context": runtime.memory_context,
                "knowledge_context": runtime.knowledge_context,
            }
        )


def build_default_invoker_registry(settings, local_functions: LocalFunctionRegistry | None = None):
    registry = AgentInvokerRegistry()
    registry.register("mock", MockAgentInvoker())
    registry.register("http", HttpAgentInvoker(settings))
    registry.register("local_function", LocalFunctionInvoker(local_functions))
    registry.register("ui_handoff", UiHandoffInvoker())
    return registry


def _validate_output(definition, result: AgentInvocationResult) -> AgentInvocationResult:
    schema = definition.output_schema.model_dump(exclude_none=True)
    if result.output is None or not schema.get("properties"):
        return result
    try:
        validate_json_schema(instance=result.output, schema=schema)
    except JsonSchemaValidationError as exc:
        return result.model_copy(
            update={
                "status": "invalid_output",
                "error": ErrorDetail(
                    code="invalid_output",
                    message="Agent output does not match output_schema",
                    details={"error": exc.message},
                ),
            }
        )
    return result


def build_invocation_input(definition, text: str | None = None, values: dict | None = None) -> dict:
    input_values = dict(values or {})
    if text and "text" in definition.input_schema.required and not input_values.get("text"):
        input_values["text"] = text
    if text:
        for key in definition.input_schema.properties:
            if key not in input_values and key in {"query", "title"}:
                input_values[key] = text
    return input_values


def missing_required_inputs(definition, invocation_input: dict) -> list[str]:
    return [item for item in definition.input_schema.required if not invocation_input.get(item)]


def _context_str(context: dict, key: str) -> str | None:
    value = context.get(key)
    return str(value) if value is not None else None


def _query_text(input_values: dict) -> str:
    for key in ("text", "query", "title"):
        value = input_values.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return " ".join(str(value) for value in input_values.values() if isinstance(value, str))


def _has_memory_context(context: MemoryContext) -> bool:
    return bool(
        context.summary
        or context.items
        or context.status != "empty"
        or context.truncated
        or context.errors
        or context.metadata
    )


def _has_knowledge_context(context: KnowledgeContext) -> bool:
    return bool(
        context.summary
        or context.items
        or context.citations
        or context.source_ids
        or context.status != "disabled"
        or context.truncated
        or context.errors
        or context.metadata
    )
