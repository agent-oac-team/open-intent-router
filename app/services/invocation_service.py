import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema

from app.core.errors import AgentUnavailableError, InvocationError
from app.core.memory_runtime import MemoryRuntimePolicy, build_memory_runtime_policy
from app.invokers.http import HttpAgentInvoker
from app.invokers.local_function import LocalFunctionInvoker, LocalFunctionRegistry
from app.invokers.mock import MockAgentInvoker
from app.invokers.registry import AgentInvokerRegistry
from app.invokers.ui_handoff import UiHandoffInvoker
from app.schemas.agent_context import KnowledgeContext, MemoryContext
from app.schemas.agents import AgentDefinition
from app.schemas.common import ErrorDetail
from app.schemas.invocation import AgentInvocation, AgentInvocationResult, InvokeRequest
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.routing import RouteRequest, RouteResponse
from app.schemas.turns import FormationEligibilitySnapshot
from app.services.memory_formation import formation_turn_id, request_prohibits_memory
from app.services.memory_integration import (
    StructuredFormationSink,
    TurnCaptureSink,
    publish_run_transitions,
)
from app.services.registry_service import AgentRegistryService


class InvocationService:
    def __init__(
        self,
        registry: AgentRegistryService,
        run_repository,
        result_repository,
        invokers: AgentInvokerRegistry,
        agent_context_service=None,
        plan_service=None,
        turn_capture: TurnCaptureSink | None = None,
        structured_formation: StructuredFormationSink | None = None,
        plan_claim_lease_seconds: float = 300,
        memory_service=None,
        canonical_invocation_store=None,
        runtime_policy: MemoryRuntimePolicy | None = None,
        memory_formation_policy_version: str = "formation-policy-v1",
    ) -> None:
        self.registry = registry
        self.run_repository = run_repository
        self.result_repository = result_repository
        self.invokers = invokers
        self.agent_context_service = agent_context_service
        self.plan_service = plan_service
        self.turn_capture = turn_capture
        self.structured_formation = structured_formation
        self.plan_claim_lease_seconds = plan_claim_lease_seconds
        self.memory_service = memory_service or getattr(
            agent_context_service, "memory_service", None
        )
        self.canonical_invocation_store = canonical_invocation_store
        self.runtime_policy = runtime_policy or build_memory_runtime_policy(
            "on" if turn_capture is not None or structured_formation is not None else "off",
            config_source="service_composition",
        )
        self.automatic_formation_enabled = self.runtime_policy.effective_formation_mode != "off"
        self.memory_formation_policy_version = memory_formation_policy_version

    async def invoke(self, request: InvokeRequest) -> AgentInvocationResult:
        definitions = await self.registry.available_definitions(request.user)
        definition = next(
            (item for item in definitions if item.agent_id == request.agent_id),
            None,
        )
        if definition is None:
            raise AgentUnavailableError(f"Agent is not available: {request.agent_id}")
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
            knowledge_context_handle=request.knowledge_context_handle,
            knowledge_context_trace_id=request.knowledge_context_trace_id,
        )
        return await self._invoke_definition(definition, invocation)

    async def issue_controlled_knowledge_context_handle(
        self,
        *,
        agent_id: str,
        user,
        variables: Mapping[str, object],
        trace_id: str,
        caller_id: str | None = None,
    ) -> str:
        """Prepare a trusted one-time Knowledge Context for a later Invocation."""
        definition = await self.registry.get_definition(agent_id)
        if definition is None:
            raise InvocationError(f"Agent not found: {agent_id}")
        if self.agent_context_service is None:
            raise InvocationError("Agent Context service is not configured")
        return await self.agent_context_service.issue_controlled_knowledge_handle(
            agent=definition,
            user=user,
            variables=variables,
            trace_id=trace_id,
            caller_id=caller_id,
        )

    async def invoke_agent(
        self,
        *,
        agent_id: str,
        session_id: str,
        user,
        input: dict,
        context: dict | None = None,
        request_id: str | None = None,
        knowledge_context_handle: str | None = None,
        knowledge_context_trace_id: str | None = None,
        selected_definition: AgentDefinition | None = None,
    ) -> AgentInvocationResult:
        definition = selected_definition or await self.registry.get_definition(agent_id)
        if definition is None:
            raise InvocationError(f"Agent not found: {agent_id}")
        if definition.agent_id != agent_id:
            raise InvocationError("Selected Agent definition does not match invocation target")
        invocation = AgentInvocation(
            run_id=f"run_{uuid4().hex}",
            request_id=request_id,
            session_id=session_id,
            agent_id=agent_id,
            user=user,
            input=input,
            context=context or {},
            knowledge_context_handle=knowledge_context_handle,
            knowledge_context_trace_id=knowledge_context_trace_id,
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
        if preview.agent_id not in route_response.context.candidate_agent_ids:
            raise AgentUnavailableError(f"Agent is not available: {preview.agent_id}")
        definition = route_response.selected_definition(preview.agent_id)
        if definition is None:
            raise AgentUnavailableError(f"Agent is not available: {preview.agent_id}")
        invocation = AgentInvocation(
            run_id=f"run_{uuid4().hex}",
            request_id=route_response.request_id,
            session_id=route_response.session_id,
            agent_id=preview.agent_id,
            user=route_request.user,
            input=preview.input,
            context={
                "route_reason": route_response.decision.reason,
                **preview.metadata,
                "_canonical_turn_managed": self.canonical_invocation_store is not None,
                "_canonical_response_text": (
                    route_response.assistant_message or route_response.decision.message or ""
                ),
            },
        )
        return await self._invoke_definition(definition, invocation)

    async def _invoke_definition(
        self,
        definition,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        invocation = await self._with_agent_context(definition, invocation)
        request_suppressed = request_prohibits_memory(invocation)
        canonical_managed = bool(invocation.context.get("_canonical_turn_managed"))
        if canonical_managed and self.canonical_invocation_store is None:
            raise InvocationError("Canonical invocation persistence is not configured")
        formation_suppressed = request_suppressed or (
            not canonical_managed and not self.automatic_formation_enabled
        )
        started = time.perf_counter()
        started_at = datetime.now(UTC)
        run = AgentRun(
            run_id=invocation.run_id,
            request_id=invocation.request_id or invocation.run_id,
            session_id=invocation.session_id,
            agent_id=invocation.agent_id,
            user_id=invocation.user.id,
            tenant_id=invocation.user.tenant_id,
            plan_id=_context_str(invocation.context, "plan_id"),
            step_id=_context_str(invocation.context, "step_id"),
            status="running",
            invoker_type=definition.type,
            input=invocation.input,
            formation_suppressed=formation_suppressed,
            used_memory_ids=[item.memory_id for item in invocation.memory_context.items],
            created_at=started_at,
            updated_at=started_at,
        )
        replay_result = None
        if canonical_managed:
            proposed_run_id = run.run_id
            run, _, replay_result = await self.canonical_invocation_store.start_run(run)
            if run.run_id != proposed_run_id and replay_result is None:
                raise InvocationError("Canonical invocation is already in progress")
        else:
            run = await self.run_repository.add_run(run)
        if replay_result is not None:
            return _invocation_result_from_record(replay_result)
        await self._link_router_recall(invocation)
        await self._publish_run(
            run,
            event_type="create",
            suppressed=formation_suppressed,
        )
        invoker = self.invokers.get(definition.type)
        try:
            result = await self._invoke_with_claim_heartbeat(invoker, definition, invocation)
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
        completed_run = run.model_copy(
            update={
                "status": result.status,
                "output": result.output,
                "error": result.error.model_dump() if result.error else None,
                "latency_ms": latency_ms,
                "updated_at": datetime.now(UTC),
            }
        )
        result_record = AgentResult(
            result_id=f"result_{uuid4().hex}",
            run_id=invocation.run_id,
            session_id=invocation.session_id,
            agent_id=definition.agent_id,
            user_id=completed_run.user_id,
            tenant_id=completed_run.tenant_id,
            turn_id=completed_run.turn_id,
            plan_id=_context_str(invocation.context, "plan_id"),
            step_id=_context_str(invocation.context, "step_id"),
            status=result.status,
            message=result.message,
            formation_suppressed=formation_suppressed,
            formation_skip_audit_required=(request_suppressed and self.automatic_formation_enabled),
            output=result.output,
            artifact_refs=[ref.model_dump() for ref in result.artifact_refs],
            error=result.error.model_dump() if result.error else None,
            created_at=datetime.now(UTC),
        )
        if canonical_managed:
            completed_run, stored_result, _ = await self.canonical_invocation_store.complete_run(
                run=completed_run,
                result=result_record,
                response_text=(
                    _context_str(invocation.context, "_canonical_response_text") or result.message
                ),
                eligibility=self._formation_eligibility(request_suppressed),
            )
        else:
            completed_run = await self.run_repository.update_run(completed_run)
            stored_result = await self.result_repository.add_result(result_record)
        await self._publish_run(
            completed_run,
            event_type=_run_event_type(result.status),
            suppressed=formation_suppressed,
        )
        await self._publish_result(
            stored_result,
            run=completed_run,
            suppressed=formation_suppressed,
        )
        await self._finish_plan_step(
            invocation,
            result,
            suppress_formation=formation_suppressed,
        )
        if not canonical_managed:
            await self._capture_turn(
                invocation=invocation,
                result=result,
                result_id=stored_result.result_id,
            )
        return result

    def _formation_eligibility(self, request_suppressed: bool) -> FormationEligibilitySnapshot:
        mode = self.runtime_policy.effective_formation_mode
        if mode not in {"off", "observe", "enforced"}:
            mode = "off"
        suppressed = request_suppressed or mode == "off"
        reason = (
            "temporary_request"
            if request_suppressed
            else ("formation_mode_off" if mode == "off" else None)
        )
        return FormationEligibilitySnapshot(
            mode=mode,
            execution_mode=self.runtime_policy.execution_plane,
            suppressed=suppressed,
            reason_code=reason,
            policy_version=self.memory_formation_policy_version,
        )

    async def _link_router_recall(self, invocation: AgentInvocation) -> None:
        if self.memory_service is None or not invocation.user.tenant_id:
            return
        request_id = invocation.request_id or invocation.run_id
        turn_id = formation_turn_id(
            tenant_id=invocation.user.tenant_id,
            user_id=invocation.user.id,
            session_id=invocation.session_id,
            request_id=request_id,
            run_id=invocation.run_id,
        )
        await self.memory_service.link_router_recall_usage(
            user_id=invocation.user.id,
            tenant_id=invocation.user.tenant_id,
            request_id=invocation.request_id,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            turn_id=turn_id,
        )

    async def _capture_turn(
        self,
        *,
        invocation: AgentInvocation,
        result: AgentInvocationResult,
        result_id: str,
    ) -> None:
        if self.turn_capture is None:
            return
        try:
            await self.turn_capture.capture(
                invocation=invocation,
                result=result,
                result_id=result_id,
            )
            marker = getattr(self.result_repository, "mark_turn_captured", None)
            if marker is not None:
                await marker(result_id)
        except Exception:
            return

    async def _publish_run(
        self,
        run: AgentRun,
        *,
        event_type: str,
        suppressed: bool = False,
    ) -> None:
        if self.structured_formation is None or suppressed:
            return
        try:
            del event_type
            await publish_run_transitions(
                publisher=self.structured_formation,
                repository=self.run_repository,
                run=run,
            )
        except Exception:
            return

    async def _publish_result(
        self,
        result: AgentResult,
        *,
        run: AgentRun,
        suppressed: bool = False,
    ) -> None:
        if self.structured_formation is None or suppressed:
            return
        try:
            await self.structured_formation.publish_result(result, run=run)
            marker = getattr(self.result_repository, "mark_formation_published", None)
            if marker is not None:
                await marker(result.result_id)
        except Exception:
            return

    async def _with_agent_context(
        self,
        definition,
        invocation: AgentInvocation,
    ) -> AgentInvocation:
        active_plan = None
        plan_id = _context_str(invocation.context, "plan_id")
        if plan_id:
            if self.plan_service is None or not invocation.user.tenant_id:
                raise InvocationError("Plan execution requires trusted ownership")
            candidate = await self.plan_service.get_plan(
                plan_id,
                tenant_id=invocation.user.tenant_id,
                user_id=invocation.user.id,
            )
            if candidate is None or candidate.status not in {"pending", "running", "blocked"}:
                raise InvocationError("Plan is not active")
            step = _current_plan_step(candidate)
            if step is None or step.agent_id != definition.agent_id:
                raise InvocationError("Agent does not match the canonical Plan step")
            claim = await self.plan_service.claim_step(
                candidate.plan_id,
                step.step_id,
                tenant_id=candidate.tenant_id,
                user_id=candidate.user_id,
                lease_seconds=self.plan_claim_lease_seconds,
                publish=not request_prohibits_memory(invocation),
            )
            if claim is None:
                raise InvocationError("Plan step is already executing")
            active_plan, claim_id = claim
            execution_key = await self.plan_service.get_execution_claim_key(
                active_plan.plan_id,
                claim_id=claim_id,
            )
            if execution_key is None:
                raise InvocationError("Plan execution claim is unavailable")
            invocation = invocation.model_copy(
                update={
                    "context": {
                        **invocation.context,
                        "plan_id": active_plan.plan_id,
                        "step_id": step.step_id,
                        "plan_status": active_plan.status,
                        "plan_execution_claim_id": claim_id,
                        "plan_execution_idempotency_key": execution_key,
                    }
                }
            )
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
            request_id=invocation.request_id,
            run_id=invocation.run_id,
            turn_id=(
                formation_turn_id(
                    tenant_id=invocation.user.tenant_id,
                    user_id=invocation.user.id,
                    session_id=invocation.session_id,
                    request_id=invocation.request_id or invocation.run_id,
                    run_id=invocation.run_id,
                )
                if invocation.user.tenant_id
                else None
            ),
            active_plan=active_plan,
            knowledge_context_handle=invocation.knowledge_context_handle,
            knowledge_context_trace_id=invocation.knowledge_context_trace_id,
        )
        return invocation.model_copy(
            update={
                "input": input_values,
                "memory_context": runtime.memory_context,
                "knowledge_context": runtime.knowledge_context,
                "knowledge_context_handle": None,
                "knowledge_context_trace_id": None,
            }
        )

    async def _invoke_with_claim_heartbeat(self, invoker, definition, invocation):
        plan_id = _context_str(invocation.context, "plan_id")
        claim_id = _context_str(invocation.context, "plan_execution_claim_id")
        tenant_id = invocation.user.tenant_id
        if not plan_id or not claim_id or not tenant_id or self.plan_service is None:
            return await invoker.invoke(definition, invocation)
        stopped = asyncio.Event()

        async def heartbeat() -> None:
            interval = max(0.01, self.plan_claim_lease_seconds / 3)
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=interval)
                    return
                except TimeoutError:
                    try:
                        renewed = await self.plan_service.renew_step_claim(
                            plan_id,
                            tenant_id=tenant_id,
                            user_id=invocation.user.id,
                            claim_id=claim_id,
                            lease_seconds=self.plan_claim_lease_seconds,
                        )
                    except Exception:
                        continue
                    if not renewed:
                        return

        task = asyncio.create_task(heartbeat(), name=f"plan-claim-heartbeat:{plan_id}")
        try:
            return await invoker.invoke(definition, invocation)
        finally:
            stopped.set()
            await task

    async def _finish_plan_step(
        self,
        invocation: AgentInvocation,
        result: AgentInvocationResult,
        suppress_formation: bool,
    ) -> None:
        plan_id = _context_str(invocation.context, "plan_id")
        step_id = _context_str(invocation.context, "step_id")
        claim_id = _context_str(invocation.context, "plan_execution_claim_id")
        tenant_id = invocation.user.tenant_id
        if not plan_id or not step_id or not claim_id or not tenant_id or self.plan_service is None:
            return
        await self.plan_service.finish_claimed_step(
            plan_id,
            step_id,
            tenant_id=tenant_id,
            user_id=invocation.user.id,
            claim_id=claim_id,
            status=_result_step_status(result.status),
            publish=not suppress_formation,
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


def _run_event_type(status: str) -> str:
    if status == "completed":
        return "complete"
    if status in {"failed", "invalid_output"}:
        return "fail"
    return "update"


def _run_source_order(status: str) -> int:
    if status == "running":
        return 1
    if status in {"blocked", "clarify"}:
        return 2
    return 3


def _result_step_status(status: str) -> str:
    if status == "completed":
        return "completed"
    if status in {"blocked", "clarify"}:
        return "blocked"
    return "failed"


def _current_plan_step(plan):
    if not plan.current_step_id:
        return None
    return next((step for step in plan.steps if step.step_id == plan.current_step_id), None)


def _invocation_result_from_record(result: AgentResult) -> AgentInvocationResult:
    return AgentInvocationResult.model_validate(
        {
            "run_id": result.run_id,
            "agent_id": result.agent_id,
            "status": result.status,
            "message": result.message,
            "output": result.output,
            "artifact_refs": result.artifact_refs,
            "error": result.error,
        }
    )


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
