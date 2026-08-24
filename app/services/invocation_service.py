import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema

from app.core.errors import (
    AgentUnavailableError,
    InvocationBindingUnavailableError,
    InvocationDeadlineExceededError,
    InvocationError,
    InvocationPreflightRejectedError,
)
from app.core.memory_runtime import MemoryRuntimePolicy, build_memory_runtime_policy
from app.repositories.interfaces import InvocationCompletionStore
from app.repositories.invocation_completion import build_invocation_completion_store
from app.runtime.invocation import (
    AgentCallEnvelope,
    InvocationDeadline,
    InvocationRuntime,
    RawInvocationFailure,
    RuntimeAdapterExecution,
    RuntimeExecutionIdentity,
)
from app.schemas.agent_context import KnowledgeContext, MemoryContext
from app.schemas.agents import AgentDefinitionV2, AgentHandlingKind
from app.schemas.common import ErrorDetail
from app.schemas.execution_traces import ExecutionTraceEventDraft, trace_id_for_turn
from app.schemas.invocation import (
    AgentInvocation,
    AgentInvocationResult,
    InvocationCancelResponse,
    InvokeRequest,
)
from app.schemas.logs import AgentResult, AgentRun, InvocationBindingSnapshot
from app.schemas.plans import Plan
from app.schemas.routing import RouteRequest, RouteResponse
from app.schemas.turns import FormationEligibilitySnapshot
from app.services.agent_context_service import KnowledgeRequirementError
from app.services.binding_resolution import BindingResolver, ResolvedInvocationBinding
from app.services.execution_trace_service import ExecutionTraceService
from app.services.memory_formation import formation_turn_id, request_prohibits_memory
from app.services.memory_integration import (
    StructuredFormationSink,
    TurnCaptureSink,
    publish_run_transitions,
)
from app.services.registry_service import AgentRegistryService
from app.services.registry_snapshot import RegistrySnapshotRuntime, RegistrySnapshotSelection

_DIRECT_INVOKE_RESERVED_CONTEXT_KEYS = frozenset(
    {
        "plan_id",
        "step_id",
        "plan_status",
        "plan_execution_claim_id",
        "plan_execution_idempotency_key",
    }
)


@dataclass(frozen=True)
class _PlanClaimReservation:
    """The reversible Plan mutation that precedes durable Run acceptance."""

    plan_before_claim: Plan | None
    step_id: str
    claim_id: str
    invocation: AgentInvocation
    fenced_recovery: bool = False


@dataclass(frozen=True)
class _DurableStartLookup:
    """The only safe outcomes when a durable Run start acknowledgement fails."""

    run: AgentRun | None
    readable: bool


class _FencedPlanRunRecovered(Exception):
    """Internal control flow for a restart-safe, no-redispatch convergence."""

    def __init__(self, result: AgentInvocationResult) -> None:
        self.result = result


class _DurableRunStartOwnedElsewhere(Exception):
    """A concurrent worker won the stable Run start; never reconcile it as ours."""


class InvocationService:
    def __init__(
        self,
        registry: AgentRegistryService,
        run_repository,
        result_repository,
        agent_context_service=None,
        plan_service=None,
        turn_capture: TurnCaptureSink | None = None,
        structured_formation: StructuredFormationSink | None = None,
        plan_claim_lease_seconds: float = 300,
        memory_service=None,
        canonical_invocation_store=None,
        runtime_policy: MemoryRuntimePolicy | None = None,
        memory_formation_policy_version: str = "formation-policy-v1",
        snapshot_runtime: RegistrySnapshotRuntime | None = None,
        binding_resolver: BindingResolver | None = None,
        execution_traces: ExecutionTraceService | None = None,
        invocation_runtime: InvocationRuntime | None = None,
        completion_store: InvocationCompletionStore | None = None,
    ) -> None:
        # The service no longer reads Native Registry records directly. Keep
        # this argument temporarily so host composition can be migrated
        # independently.  A composition may publish an already-built Snapshot
        # and resolver beside that service; every execution path below still
        # consumes the resulting Snapshot selection instead of reading Registry
        # records or dispatching by an obsolete handling type.
        published_snapshot_runtime = getattr(registry, "snapshot_runtime", None)
        published_binding_resolver = getattr(registry, "binding_resolver", None)
        if snapshot_runtime is None and isinstance(
            published_snapshot_runtime, RegistrySnapshotRuntime
        ):
            snapshot_runtime = published_snapshot_runtime
        if binding_resolver is None and isinstance(published_binding_resolver, BindingResolver):
            binding_resolver = published_binding_resolver
        del registry
        self.run_repository = run_repository
        self.result_repository = result_repository
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
        self.snapshot_runtime = snapshot_runtime
        self.binding_resolver = binding_resolver
        self.execution_traces = execution_traces
        # Production composition creates this once for the application
        # lifespan.  The local fallback keeps direct service construction a
        # usable test seam while never constructing an Adapter per request.
        self.invocation_runtime = invocation_runtime or InvocationRuntime()
        self.completion_store = completion_store or build_invocation_completion_store(
            run_repository=run_repository,
            result_repository=result_repository,
        )

    async def invoke(self, request: InvokeRequest) -> AgentInvocationResult:
        snapshot_runtime = self.snapshot_runtime
        if snapshot_runtime is None or snapshot_runtime.snapshot is None:
            raise InvocationBindingUnavailableError(
                "Registry Snapshot is unavailable for Invocation",
                details={"reason_code": "registry_snapshot_unavailable"},
            )
        selection = snapshot_runtime.preflight_for_user(request.agent_id, request.user)
        if selection is None:
            raise AgentUnavailableError(f"Agent is not available: {request.agent_id}")
        resolved = self.resolve_direct_binding(selection)
        invocation = AgentInvocation(
            run_id=f"run_{uuid4().hex}",
            request_id=request.request_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            user=request.user,
            input=request.input,
            artifact_refs=request.artifact_refs,
            context=_v2_direct_invocation_context(request.context),
            memory_context=request.memory_context or MemoryContext(),
            knowledge_context=request.knowledge_context or KnowledgeContext(),
            knowledge_context_handle=request.knowledge_context_handle,
            knowledge_context_trace_id=request.knowledge_context_trace_id,
        )
        return await self._invoke_resolved_binding(resolved, invocation)

    async def cancel_run(
        self,
        run_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> InvocationCancelResponse | None:
        """Request truthful stop control for one owned Runtime Invocation Run.

        Durable ownership is always read before touching the process Runtime.
        A stale process record, a non-Invocation Run, or a post-restart running
        Run never grants an Adapter control call merely because a caller knows
        a Run id.
        """

        run = await self.run_repository.get_owned_run(
            run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if run is None:
            return None
        if run.status != "running":
            return InvocationCancelResponse(
                run_id=run.run_id,
                run_status=run.status,
                control_state="cancelled" if run.status == "cancelled" else "terminal",
            )
        if run.handling_kind != "invocation" or not isinstance(
            run.binding_snapshot,
            InvocationBindingSnapshot,
        ):
            return InvocationCancelResponse(
                run_id=run.run_id,
                run_status=run.status,
                control_state="unsupported",
                completion_certainty="unknown",
                reason_code="control_unsupported",
            )
        decision = await self.invocation_runtime.request_cancellation(
            RuntimeExecutionIdentity(
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                session_id=run.session_id,
                agent_id=run.agent_id,
            )
        )
        return InvocationCancelResponse(
            run_id=run.run_id,
            run_status=run.status,
            control_state=decision.control_state,
            completion_certainty=decision.completion_certainty,
            reason_code=decision.reason_code,
        )

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
        snapshot_runtime = self.snapshot_runtime
        if snapshot_runtime is None or snapshot_runtime.snapshot is None:
            raise InvocationBindingUnavailableError(
                "Registry Snapshot is unavailable for Invocation",
                details={"reason_code": "registry_snapshot_unavailable"},
            )
        selection = snapshot_runtime.preflight_for_user(agent_id, user)
        if selection is None:
            raise InvocationError(f"Agent not found: {agent_id}")
        if self.agent_context_service is None:
            raise InvocationError("Agent Context service is not configured")
        return await self.agent_context_service.issue_controlled_knowledge_handle(
            agent=selection.definition,
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
        selected_definition: AgentDefinitionV2 | None = None,
        selected_binding: RegistrySnapshotSelection | None = None,
        resolved_binding: ResolvedInvocationBinding | None = None,
    ) -> AgentInvocationResult:
        if resolved_binding is not None:
            definition = resolved_binding.definition
            if (
                selected_binding is not None
                and selected_binding.definition.agent_id != definition.agent_id
            ):
                raise InvocationError("Resolved Binding does not match invocation target")
        elif selected_binding is not None:
            definition = selected_binding.definition
            if (
                selected_definition is not None
                and selected_definition.agent_id != definition.agent_id
            ):
                raise InvocationError("Selected Binding does not match invocation target")
        else:
            definition = selected_definition
        if definition is None:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "plan_binding_unavailable"},
            )
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
        if resolved_binding is not None:
            return await self._invoke_resolved_binding(resolved_binding, invocation)
        if selected_binding is not None:
            return await self._invoke_resolved_binding(
                self.resolve_direct_binding(selected_binding),
                invocation,
            )
        raise InvocationBindingUnavailableError(
            "Invocation Binding is unavailable",
            details={"reason_code": "plan_binding_unavailable"},
        )

    def resolve_direct_binding(
        self,
        selection: RegistrySnapshotSelection,
    ) -> ResolvedInvocationBinding:
        """Resolve a trusted v2 selection before accepting any execution state."""

        if self.binding_resolver is None:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "binding_resolver_unavailable"},
            )
        return self.binding_resolver.resolve_direct_invocation(selection)

    async def invoke_from_route(
        self,
        route_request: RouteRequest,
        route_response: RouteResponse,
    ) -> AgentInvocationResult | None:
        preview = route_response.invocation
        if preview is None:
            return None
        if not route_response.has_trusted_invocation_for(route_request):
            raise AgentUnavailableError(f"Agent is not available: {preview.agent_id}")
        if preview.agent_id not in route_response.context.candidate_agent_ids:
            raise AgentUnavailableError(f"Agent is not available: {preview.agent_id}")
        invocation = AgentInvocation(
            run_id=f"run_{uuid4().hex}",
            request_id=route_response.request_id,
            session_id=route_response.session_id,
            agent_id=preview.agent_id,
            user=route_request.user,
            input=preview.input,
            artifact_refs=route_response.context.artifact_refs,
            context={
                "route_reason": route_response.decision.reason,
                **preview.metadata,
                "_canonical_turn_managed": self.canonical_invocation_store is not None,
                "_canonical_response_text": (
                    route_response.assistant_message or route_response.decision.message or ""
                ),
            },
        )
        selection = route_response.selected_binding(preview.agent_id)
        if not isinstance(selection, RegistrySnapshotSelection):
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "route_binding_unavailable"},
            )
        if selection.definition.agent_id != preview.agent_id:
            raise AgentUnavailableError(f"Agent is not available: {preview.agent_id}")
        return await self._invoke_resolved_binding(
            self.resolve_direct_binding(selection),
            invocation,
        )

    async def _invoke_resolved_binding(
        self,
        resolved: ResolvedInvocationBinding,
        invocation: AgentInvocation,
    ) -> AgentInvocationResult:
        """Perform local input checks, then own one request Connector scope."""

        # The only Invocation deadline is established before any preflight
        # dependency (including Connector resolution) gets to run.
        invocation = self.invocation_runtime.establish_deadline(invocation)
        deadline = self.invocation_runtime.deadline_for(invocation)
        definition = resolved.definition
        runtime_execution = resolved.runtime_execution
        invocation = invocation.model_copy(
            update={
                "input": self.invocation_runtime.preflight_input(
                    execution=runtime_execution,
                    invocation=invocation,
                    input_schema=definition.input_schema.model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
                    reject_reserved_request_keys=True,
                )
            }
        )

        binding_resolver = self.binding_resolver
        if binding_resolver is None:
            raise InvocationBindingUnavailableError(
                "Invocation Binding is unavailable",
                details={"reason_code": "binding_resolver_unavailable"},
            )
        # Connector resolution occurs after pure input validation but before
        # Context assembly, Plan claim, Run creation, or Adapter dispatch. The
        # entire scope executes in an independently owned task: if an HTTP
        # client disconnects after Run acceptance, the Connector stays live
        # until that accepted task safely converges its Run/Result.
        accepted = asyncio.Event()

        async def invoke_in_connector_scope() -> AgentInvocationResult:
            async with binding_resolver.open_connector(
                resolved,
                principal=invocation.user,
                deadline=deadline,
            ) as bound:
                return await self._invoke_connector_bound(
                    binding=bound,
                    invocation=invocation,
                    deadline=deadline,
                    on_accepted=accepted.set,
                )

        execution_task = asyncio.create_task(invoke_in_connector_scope())
        try:
            return await asyncio.shield(execution_task)
        except asyncio.CancelledError:
            if accepted.is_set():
                self.invocation_runtime.retain_accepted_execution(execution_task)
            else:
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
            raise

    async def _invoke_connector_bound(
        self,
        *,
        binding: ResolvedInvocationBinding,
        invocation: AgentInvocation,
        deadline: InvocationDeadline,
        on_accepted: Callable[[], None] | None = None,
    ) -> AgentInvocationResult:
        """Execute one Binding whose optional Connector is already live."""

        definition = binding.definition
        binding_snapshot = binding.persistence_snapshot
        runtime_execution = binding.runtime_execution

        def preflight(prepared_invocation: AgentInvocation) -> AgentCallEnvelope:
            return self.invocation_runtime.preflight(
                execution=runtime_execution,
                invocation=prepared_invocation,
                input_schema=definition.input_schema.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            )

        async def execute(
            prepared_invocation: AgentInvocation,
            envelope: AgentCallEnvelope,
        ) -> AgentInvocationResult:
            async def invoke_runtime(
                runtime_invocation: AgentInvocation,
            ) -> AgentInvocationResult:
                return await self.invocation_runtime.execute_preflighted(
                    execution=runtime_execution,
                    envelope=envelope,
                    invocation=runtime_invocation,
                    agent_id=definition.agent_id,
                    output_schema=definition.output_schema.model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
                )

            return await self._invoke_with_claim_heartbeat(
                invoke_runtime,
                prepared_invocation,
                deadline=deadline,
            )

        return await self._execute_accepted_invocation(
            definition,
            invocation,
            invoker_type=binding.adapter_key,
            agent_revision=definition.revision,
            handling_kind=definition.handling.kind,
            binding_snapshot=binding_snapshot,
            binding_trace_facts={
                "agent_revision": definition.revision,
                "handling_kind": definition.handling.kind,
                "binding_schema_version": binding_snapshot.schema_version,
                "adapter_contract_version": binding_snapshot.adapter_contract_version,
                "adapter_implementation_version": binding_snapshot.adapter_implementation_version,
            },
            execute=execute,
            runtime_execution=runtime_execution,
            preflight=preflight,
            release_before_terminal=binding.release_before_terminal,
            deadline=deadline,
            on_accepted=on_accepted,
        )

    async def _execute_accepted_invocation(
        self,
        definition: AgentDefinitionV2,
        invocation: AgentInvocation,
        *,
        invoker_type: str,
        execute: Callable[[AgentInvocation, AgentCallEnvelope], Awaitable[AgentInvocationResult]],
        agent_revision: int,
        handling_kind: AgentHandlingKind,
        binding_snapshot: InvocationBindingSnapshot,
        binding_trace_facts: dict[str, object],
        runtime_execution: RuntimeAdapterExecution,
        preflight: Callable[[AgentInvocation], AgentCallEnvelope],
        release_before_terminal: Callable[[], Awaitable[None]] | None = None,
        deadline: InvocationDeadline,
        on_accepted: Callable[[], None] | None = None,
    ) -> AgentInvocationResult:
        # Context assembly may read trusted state, but all Runtime rejections
        # must occur before a canonical Plan Step is claimed or a Run exists.
        deadline.require_remaining()
        invocation = await self._with_agent_context(definition, invocation, deadline=deadline)
        preflight_envelope = preflight(invocation)
        request_suppressed = request_prohibits_memory(invocation)
        canonical_managed = bool(invocation.context.get("_canonical_turn_managed"))
        if canonical_managed and self.canonical_invocation_store is None:
            raise InvocationError("Canonical invocation persistence is not configured")
        try:
            invocation, plan_claim = await self._claim_plan_execution(
                definition,
                invocation,
                deadline=deadline,
            )
        except _FencedPlanRunRecovered as recovered:
            return recovered.result
        durable_run_start_attempted = False
        late_durable_start_reconciliation_registered = False

        def reconcile_late_durable_start(
            task: asyncio.Future[object],
        ) -> None:
            """Delay commit-unknown recovery until a timed-out start stops."""

            nonlocal late_durable_start_reconciliation_registered
            late_durable_start_reconciliation_registered = True
            reconciliation = asyncio.create_task(
                self._reconcile_late_durable_start_task(
                    task=task,
                    run=run,
                    plan_claim=plan_claim,
                    deadline=deadline,
                    invocation=invocation,
                    agent_id=definition.agent_id,
                    started=started,
                    request_suppressed=request_suppressed,
                    canonical_managed=canonical_managed,
                    formation_suppressed=formation_suppressed,
                ),
                name=f"durable-start-settlement:{invocation.run_id}",
            )
            self.invocation_runtime.retain_durable_start_reconciliation(reconciliation)

        try:
            if preflight_envelope is not None:
                preflight_envelope = self.invocation_runtime.attach_trusted_plan_idempotency(
                    preflight_envelope,
                    invocation,
                )
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
                invoker_type=invoker_type,
                agent_revision=agent_revision,
                handling_kind=handling_kind,
                binding_snapshot=binding_snapshot,
                deadline_at=invocation.deadline_at,
                input=invocation.input,
                formation_suppressed=formation_suppressed,
                used_memory_ids=[item.memory_id for item in invocation.memory_context.items],
                created_at=started_at,
                updated_at=started_at,
            )
            # Model construction and trusted idempotency projection are still
            # pre-acceptance work. Recheck immediately before the first durable
            # side effect so an elapsed budget cannot create a Run.
            deadline.require_remaining()
            fenced = await deadline.wait_for(
                self._fence_plan_claim_before_durable_start(plan_claim),
                on_late_task=(
                    lambda _task: (
                        self._release_unaccepted_plan_claim(plan_claim)
                        if plan_claim is not None
                        else None
                    )
                ),
            )
            if not fenced:
                raise InvocationError("Plan execution claim is unavailable")
            # Fencing itself is a durable pre-start operation. It must not
            # create a Run after the caller's budget expired while it ran.
            deadline.require_remaining()
            # The following await is the durable Run acceptance boundary.  A
            # disconnect must not cancel a task in the gap between a repository
            # commit and an after-the-fact callback: the retained task owns either
            # commit outcome and safely releases its Connector on failure.
            if on_accepted is not None:
                on_accepted()
            deadline.require_remaining()
            replay_result = None
            durable_run_start_attempted = True
            if canonical_managed:
                run, _, replay_result, start_created = await deadline.wait_for(
                    self.canonical_invocation_store.start_run(
                        run,
                        may_commit=lambda: deadline.remaining_seconds() > 0,
                    ),
                    on_late_task=reconcile_late_durable_start,
                )
                if not start_created and replay_result is None:
                    raise _DurableRunStartOwnedElsewhere
            else:
                run, start_created = await deadline.wait_for(
                    self.run_repository.add_run_if_absent(
                        run,
                        may_commit=lambda: deadline.remaining_seconds() > 0,
                    ),
                    on_late_task=reconcile_late_durable_start,
                )
                if not start_created:
                    raise _DurableRunStartOwnedElsewhere
        except _DurableRunStartOwnedElsewhere:
            # The durable, non-expiring Plan Claim remains attached to the
            # winning Run. It is not an acknowledgement loss for this worker,
            # so no local reconciliation may terminalize the other owner's
            # Adapter execution.
            raise InvocationError("Plan step is already executing") from None
        except asyncio.CancelledError as exc:
            if not durable_run_start_attempted:
                self._defer_unaccepted_plan_claim_release(plan_claim, deadline=deadline)
                raise
            if late_durable_start_reconciliation_registered:
                raise
            # A repository cancellation can occur after commit but before the
            # caller receives its acknowledgement. Reconcile the stable Run
            # identity before deciding whether this was still pre-acceptance;
            # an observed running Run must converge to a terminal Result.
            self._retain_durable_start_reconciliation(
                run=run,
                plan_claim=plan_claim,
                deadline=deadline,
                cause=exc,
                invocation=invocation,
                agent_id=definition.agent_id,
                started=started,
                request_suppressed=request_suppressed,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            )
            raise
        except Exception as exc:
            if not durable_run_start_attempted:
                self._defer_unaccepted_plan_claim_release(plan_claim, deadline=deadline)
                raise
            if late_durable_start_reconciliation_registered:
                raise
            # A non-cancellation failure can also be an after-commit
            # acknowledgement loss. Never leave a confirmed Run running, and
            # never release a Claim while the durable read is unavailable.
            self._retain_durable_start_reconciliation(
                run=run,
                plan_claim=plan_claim,
                deadline=deadline,
                cause=exc,
                invocation=invocation,
                agent_id=definition.agent_id,
                started=started,
                request_suppressed=request_suppressed,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            )
            raise
        if replay_result is not None:
            await self._release_unaccepted_plan_claim(plan_claim)
            return _invocation_result_from_record(replay_result)
        # ``start_run`` returned in this Task, so its durable mutation and the
        # following in-memory registration are one uninterrupted event-loop
        # turn. Registering only here prevents a losing concurrent start
        # attempt from leaving a process-local pre-dispatch control record for
        # somebody else's canonical Run.
        self.invocation_runtime.register_execution_control(
            invocation=invocation,
            agent_id=definition.agent_id,
            execution=runtime_execution,
        )
        adapter_dispatch_started = False
        try:
            # All accepted-but-pre-dispatch collaborators consume the same
            # absolute budget.  The durable Claim remains fenced until the
            # terminal Plan transition, so a timeout here cannot permit an
            # automatic redispatch while the terminal result is being written.
            await deadline.wait_for(self._link_router_recall(invocation))
            await deadline.wait_for(self._record_agent_context_recall_usage(invocation))
            await deadline.wait_for(
                self._record_binding_trace(
                    invocation=invocation,
                    run=run,
                    binding_trace_facts=binding_trace_facts,
                )
            )
            await deadline.wait_for(
                self._publish_run(
                    run,
                    event_type="create",
                    suppressed=formation_suppressed,
                )
            )
            if not await self.invocation_runtime.mark_dispatch_started(invocation.run_id):
                # A control request reached this accepted Run before the
                # Adapter edge. The Runtime itself is the proof that no
                # dispatch occurred, so cancellation is certain without
                # invoking an Adapter control method.
                result = _cancelled_invocation_result(
                    invocation=invocation,
                    agent_id=definition.agent_id,
                )
            else:
                adapter_dispatch_started = True
                result = await execute(invocation, preflight_envelope)
                result = result.model_copy(
                    update={"run_id": invocation.run_id, "agent_id": definition.agent_id}
                )
                result = _validate_output(definition, result)
        except asyncio.CancelledError as exc:
            # The accepted Run may be cancelled during side-effecting Adapter
            # work or any best-effort pre-dispatch publication.  Either way,
            # it must take the same terminal persistence path instead of
            # leaving a durable ``running`` record behind at shutdown.
            result = self.invocation_runtime.project_exception(
                invocation=invocation,
                agent_id=definition.agent_id,
                exc=exc,
                completion_certainty=("unknown" if adapter_dispatch_started else "certain"),
            )
        except Exception as exc:
            result = self.invocation_runtime.project_exception(
                invocation=invocation,
                agent_id=definition.agent_id,
                exc=exc,
                completion_certainty=("unknown" if adapter_dispatch_started else "certain"),
            )
        # Once the Runtime has a result, no late control request may reach the
        # Adapter. A confirmed stop wins over a non-terminal Adapter outcome;
        # it is the only fact allowed to produce the public cancelled state.
        if await self.invocation_runtime.begin_terminalization(invocation.run_id):
            result = _cancelled_invocation_result(
                invocation=invocation,
                agent_id=definition.agent_id,
            )
        if release_before_terminal is not None:
            try:
                # Connector cleanup belongs to the same absolute Invocation
                # budget. Do it before choosing the durable terminal result,
                # so a late deployment release becomes the durable deadline
                # outcome instead of an after-the-fact response mismatch.
                await release_before_terminal()
            except asyncio.CancelledError as exc:
                result = self.invocation_runtime.project_exception(
                    invocation=invocation,
                    agent_id=definition.agent_id,
                    exc=exc,
                    completion_certainty=("unknown" if adapter_dispatch_started else "certain"),
                )
            except Exception as exc:
                result = self.invocation_runtime.project_exception(
                    invocation=invocation,
                    agent_id=definition.agent_id,
                    exc=exc,
                    completion_certainty=("unknown" if adapter_dispatch_started else "certain"),
                )
        completed = await self._await_terminal_completion(
            self._complete_accepted_invocation(
                run=run,
                result=result,
                invocation=invocation,
                started=started,
                request_suppressed=request_suppressed,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
                deadline=deadline,
            )
        )
        self.invocation_runtime.complete_execution_control(invocation.run_id)
        return completed

    async def _reconcile_late_durable_start_task(
        self,
        *,
        task: asyncio.Future[object],
        run: AgentRun,
        plan_claim: _PlanClaimReservation | None,
        deadline: InvocationDeadline,
        invocation: AgentInvocation,
        agent_id: str,
        started: float,
        request_suppressed: bool,
        canonical_managed: bool,
        formation_suppressed: bool,
    ) -> None:
        """Schedule durable-start reconciliation only after the write settles."""

        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, Exception):
            pass
        await self._reconcile_durable_start_outcome(
            run=run,
            plan_claim=plan_claim,
            deadline=deadline,
            cause=InvocationDeadlineExceededError(),
            invocation=invocation,
            agent_id=agent_id,
            started=started,
            request_suppressed=request_suppressed,
            canonical_managed=canonical_managed,
            formation_suppressed=formation_suppressed,
        )

    def _retain_durable_start_reconciliation(
        self,
        *,
        run: AgentRun,
        plan_claim: _PlanClaimReservation | None,
        deadline: InvocationDeadline,
        cause: BaseException,
        invocation: AgentInvocation,
        agent_id: str,
        started: float,
        request_suppressed: bool,
        canonical_managed: bool,
        formation_suppressed: bool,
    ) -> None:
        """Detach only no-dispatch recovery after an unknown durable start outcome."""

        task = asyncio.create_task(
            self._reconcile_durable_start_outcome(
                run=run,
                plan_claim=plan_claim,
                deadline=deadline,
                cause=cause,
                invocation=invocation,
                agent_id=agent_id,
                started=started,
                request_suppressed=request_suppressed,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            )
        )
        self.invocation_runtime.retain_durable_start_reconciliation(task)

    async def _reconcile_durable_start_outcome(
        self,
        *,
        run: AgentRun,
        plan_claim: _PlanClaimReservation | None,
        deadline: InvocationDeadline,
        cause: BaseException,
        invocation: AgentInvocation,
        agent_id: str,
        started: float,
        request_suppressed: bool,
        canonical_managed: bool,
        formation_suppressed: bool,
    ) -> None:
        """Resolve commit unknown without ever redispatching an Adapter."""

        try:
            while True:
                lookup = await self._lookup_run_after_interrupted_start(run)
                if lookup.run is not None:
                    if not _is_same_durable_start_attempt(lookup.run, run):
                        # A shared Plan execution key can race across two
                        # Canonical Turns. This worker only knows that its own
                        # acknowledgement is unknown; a different owner using
                        # the same stable Run ID must never be terminalized by
                        # this reconciliation path.
                        return
                    terminal_exception: BaseException = (
                        TimeoutError() if deadline.remaining_seconds() <= 0 else cause
                    )
                    result = self.invocation_runtime.project_exception(
                        invocation=invocation,
                        agent_id=agent_id,
                        exc=terminal_exception,
                        completion_certainty="certain",
                    )
                    await self._await_terminal_completion(
                        self._complete_accepted_invocation(
                            run=lookup.run,
                            result=result,
                            invocation=invocation,
                            started=started,
                            request_suppressed=request_suppressed,
                            canonical_managed=canonical_managed,
                            formation_suppressed=formation_suppressed,
                            deadline=deadline,
                        )
                    )
                    return
                if lookup.readable:
                    await self._release_unaccepted_plan_claim(plan_claim)
                    return
                # The Plan Claim was durably fenced before Run start. Keep
                # retrying only this local read; no branch can invoke an
                # Adapter or create a second Run while commit state is unknown.
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            # Process teardown may stop observing this local recovery. The
            # persisted non-expiring Claim remains the restart-safe fence.
            return

    async def _lookup_run_after_interrupted_start(self, run: AgentRun) -> _DurableStartLookup:
        """Read a stable Run identity without mistaking store failure for absence."""

        try:
            return _DurableStartLookup(
                run=await self.run_repository.get_run(run.run_id),
                readable=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return _DurableStartLookup(run=None, readable=False)

    async def _await_terminal_completion(
        self,
        completion: Awaitable[AgentInvocationResult],
    ) -> AgentInvocationResult:
        """Keep an accepted Run's terminal write alive across lifecycle cancellation."""

        completion_task = asyncio.create_task(completion)
        try:
            return await asyncio.shield(completion_task)
        except asyncio.CancelledError:
            # A second lifecycle cancellation must not propagate into the
            # atomic Run/Result completion. Keep it process-owned so the
            # lifecycle drain can observe the durable convergence separately.
            self.invocation_runtime.retain_accepted_execution(completion_task)
            return await asyncio.shield(completion_task)

    async def _complete_accepted_invocation(
        self,
        *,
        run: AgentRun,
        result: AgentInvocationResult,
        invocation: AgentInvocation,
        started: float,
        request_suppressed: bool,
        canonical_managed: bool,
        formation_suppressed: bool,
        deadline: InvocationDeadline | None = None,
    ) -> AgentInvocationResult:
        """Persist exactly one terminal result for an already accepted Run."""

        completed_run, result_record = self._terminal_records(
            run=run,
            result=result,
            invocation=invocation,
            started=started,
            request_suppressed=request_suppressed,
            formation_suppressed=formation_suppressed,
        )
        persistence_task = asyncio.create_task(
            self._persist_accepted_terminal(
                completed_run=completed_run,
                result_record=result_record,
                invocation=invocation,
                request_suppressed=request_suppressed,
                canonical_managed=canonical_managed,
                may_commit=(
                    (lambda: deadline.remaining_seconds() > 0) if deadline is not None else None
                ),
            ),
            name=f"accepted-terminal-persist:{run.run_id}",
        )
        late_persistence_reconciliation_registered = False

        def reconcile_late_persistence(_task: asyncio.Future[object]) -> None:
            """Keep a commit-unknown terminal write owned off request path."""

            nonlocal late_persistence_reconciliation_registered
            if late_persistence_reconciliation_registered:
                return
            late_persistence_reconciliation_registered = True
            self._retain_late_terminal_persistence(
                persistence_task=persistence_task,
                original_result=result,
                run=run,
                invocation=invocation,
                started=started,
                request_suppressed=request_suppressed,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            )

        if deadline is not None and deadline.remaining_seconds() <= 0:
            persistence_task.cancel()
            reconcile_late_persistence(persistence_task)
            return self._accepted_deadline_result(
                invocation,
                result.agent_id,
                prior_result=result,
            )
        try:
            if deadline is None:
                stored_run, stored_result = await persistence_task
            else:
                stored_run, stored_result = await deadline.wait_for(
                    asyncio.shield(persistence_task),
                    on_late_task=reconcile_late_persistence,
                    # The persistence store's commit guard is the authority
                    # for this accepted boundary. A terminal pair that
                    # committed before the deadline remains the canonical
                    # outcome even if an acknowledgement reaches this task
                    # just after the caller's clock crosses the line.
                    require_remaining_after_result=False,
                )
        except InvocationDeadlineExceededError:
            # The accepted Run is already durable, but its terminal write may
            # be between a database commit and acknowledgement.  Return the
            # conservative public outcome immediately and let a strongly
            # owned, no-dispatch continuation reconcile the stable Run id.
            # Interrupt the original persistence attempt.  The durable store
            # also receives the same commit guard, so an implementation that
            # resumes after cancellation cannot commit a stale success.
            persistence_task.cancel()
            reconcile_late_persistence(persistence_task)
            return self._accepted_deadline_result(
                invocation,
                result.agent_id,
                prior_result=result,
            )
        except asyncio.CancelledError:
            # A client disconnect or bounded application shutdown must not
            # cancel-and-forget a terminal transaction.  The retained
            # continuation will read/replay or safely finish it.
            reconcile_late_persistence(persistence_task)
            raise

        return await self._finalize_persisted_accepted_invocation(
            completed_run=stored_run,
            stored_result=stored_result,
            result=result,
            invocation=invocation,
            canonical_managed=canonical_managed,
            formation_suppressed=formation_suppressed,
            deadline=deadline,
        )

    def _terminal_records(
        self,
        *,
        run: AgentRun,
        result: AgentInvocationResult,
        invocation: AgentInvocation,
        started: float,
        request_suppressed: bool,
        formation_suppressed: bool,
    ) -> tuple[AgentRun, AgentResult]:
        """Build the durable terminal records without beginning I/O."""

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
            agent_id=result.agent_id,
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
        return completed_run, result_record

    async def _persist_accepted_terminal(
        self,
        *,
        completed_run: AgentRun,
        result_record: AgentResult,
        invocation: AgentInvocation,
        request_suppressed: bool,
        canonical_managed: bool,
        may_commit: Callable[[], bool] | None = None,
    ) -> tuple[AgentRun, AgentResult]:
        """Write a Run/Result pair once; callers own deadline observation."""

        if canonical_managed:
            stored_run, stored_result, _ = await self.canonical_invocation_store.complete_run(
                run=completed_run,
                result=result_record,
                response_text=(
                    _context_str(invocation.context, "_canonical_response_text")
                    or result_record.message
                ),
                eligibility=self._formation_eligibility(request_suppressed),
                may_commit=may_commit,
            )
            return stored_run, stored_result
        stored_run, stored_result = await self.completion_store.complete(
            run=completed_run,
            result=result_record,
            may_commit=may_commit,
        )
        return stored_run, stored_result

    async def _finalize_persisted_accepted_invocation(
        self,
        *,
        completed_run: AgentRun,
        stored_result: AgentResult,
        result: AgentInvocationResult,
        invocation: AgentInvocation,
        canonical_managed: bool,
        formation_suppressed: bool,
        deadline: InvocationDeadline | None,
    ) -> AgentInvocationResult:
        """Run non-critical terminal follow-up without extending the response."""

        tail = asyncio.create_task(
            self._finalize_accepted_invocation(
                completed_run=completed_run,
                stored_result=stored_result,
                result=result,
                invocation=invocation,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            ),
            name=f"accepted-terminal-follow-up:{completed_run.run_id}",
        )
        if deadline is None:
            await tail
            return result
        # The durable Run/Result pair above is the response-critical accepted
        # state. Publication, Plan fan-out and capture are intentionally
        # follow-up work: retaining them immediately avoids an unbounded
        # publisher/capture implementation turning an otherwise on-time
        # invocation into a late public success or failure.
        self.invocation_runtime.retain_accepted_execution(tail)
        return result

    def _accepted_deadline_result(
        self,
        invocation: AgentInvocation,
        agent_id: str,
        *,
        prior_result: AgentInvocationResult | None = None,
    ) -> AgentInvocationResult:
        """Project the only safe response once an accepted call expires."""

        if (
            prior_result is not None
            and prior_result.error is not None
            and prior_result.error.code == "invocation_deadline_exceeded"
            and prior_result.error.details.get("completion_certainty") == "certain"
        ):
            # The Adapter was never called (for example an accepted-but-
            # pre-dispatch publication consumed the last budget). Preserve
            # that stronger fact instead of widening it to unknown merely
            # because the terminal storage attempt itself is now late.
            return prior_result
        return self.invocation_runtime.project_failure(
            invocation=invocation,
            agent_id=agent_id,
            failure=RawInvocationFailure.for_category(
                "deadline_exceeded",
                retryable=False,
            ),
            completion_certainty="unknown",
        )

    def _retain_late_terminal_persistence(
        self,
        *,
        persistence_task: asyncio.Future[tuple[AgentRun, AgentResult]],
        original_result: AgentInvocationResult,
        run: AgentRun,
        invocation: AgentInvocation,
        started: float,
        request_suppressed: bool,
        canonical_managed: bool,
        formation_suppressed: bool,
    ) -> None:
        """Retain commit-unknown terminal recovery through application stop."""

        # Retain the actual persistence task as well as its reconciliation
        # wrapper.  A lifecycle stop cancels/drains both; shielding the write
        # inside the wrapper alone would otherwise let it keep using storage
        # after the application has disposed its dependencies.
        self.invocation_runtime.retain_accepted_execution(persistence_task)
        task = asyncio.create_task(
            self._reconcile_late_terminal_persistence(
                persistence_task=persistence_task,
                original_result=original_result,
                run=run,
                invocation=invocation,
                started=started,
                request_suppressed=request_suppressed,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            ),
            name=f"accepted-terminal-reconcile:{run.run_id}",
        )
        self.invocation_runtime.retain_accepted_execution(task)

    async def _reconcile_late_terminal_persistence(
        self,
        *,
        persistence_task: asyncio.Future[tuple[AgentRun, AgentResult]],
        original_result: AgentInvocationResult,
        run: AgentRun,
        invocation: AgentInvocation,
        started: float,
        request_suppressed: bool,
        canonical_managed: bool,
        formation_suppressed: bool,
    ) -> None:
        """Converge a terminal write after a deadline without redispatching.

        The initial task remains shielded, so it either returns its durable
        pair or eventually exposes an error.  Only after it stops may the
        continuation inspect the stable Run: a readable ``running`` Run can
        safely receive the conservative deadline terminal record; a terminal
        Run is never overwritten merely because its acknowledgement was lost.
        """

        try:
            stored_run, stored_result = await asyncio.shield(persistence_task)
        except asyncio.CancelledError:
            if not persistence_task.done():
                # This reconciliation itself was cancelled by application
                # shutdown. The separately retained persistence task still
                # owns the eventual commit/cancellation outcome.
                return
            stored_run = None
            stored_result = None
        except Exception:
            stored_run = None
            stored_result = None
        if stored_run is not None and stored_result is not None:
            await self._finalize_accepted_invocation(
                completed_run=stored_run,
                stored_result=stored_result,
                result=original_result,
                invocation=invocation,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            )
            return

        deadline_result = self._accepted_deadline_result(
            invocation,
            original_result.agent_id,
            prior_result=original_result,
        )
        while True:
            lookup = await self._lookup_run_after_interrupted_start(run)
            if not lookup.readable:
                await asyncio.sleep(0.05)
                continue
            if lookup.run is None:
                # An accepted start wrote this identity before terminal work;
                # absence is only possible if storage is still converging.
                await asyncio.sleep(0.05)
                continue
            if lookup.run.status != "running":
                # A post-commit acknowledgement error left a terminal Run.
                # Preserve that durable fact; the Plan completion path is
                # idempotent and can safely clear its fenced Claim without
                # calling an Adapter or inventing a second Result.
                await self._finish_plan_step(
                    invocation,
                    _result_from_terminal_run(lookup.run),
                    suppress_formation=formation_suppressed,
                )
                return
            fallback_run, fallback_record = self._terminal_records(
                run=lookup.run,
                result=deadline_result,
                invocation=invocation,
                started=started,
                request_suppressed=request_suppressed,
                formation_suppressed=formation_suppressed,
            )
            try:
                stored_run, stored_result = await self._persist_accepted_terminal(
                    completed_run=fallback_run,
                    result_record=fallback_record,
                    invocation=invocation,
                    request_suppressed=request_suppressed,
                    canonical_managed=canonical_managed,
                    may_commit=None,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(0.05)
                continue
            await self._finalize_accepted_invocation(
                completed_run=stored_run,
                stored_result=stored_result,
                result=deadline_result,
                invocation=invocation,
                canonical_managed=canonical_managed,
                formation_suppressed=formation_suppressed,
            )
            return

    async def _finalize_accepted_invocation(
        self,
        *,
        completed_run: AgentRun,
        stored_result: AgentResult,
        result: AgentInvocationResult,
        invocation: AgentInvocation,
        canonical_managed: bool,
        formation_suppressed: bool,
    ) -> None:
        """Persist non-response-critical follow-up after a terminal Run/Result."""

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

    async def _record_binding_trace(
        self,
        *,
        invocation: AgentInvocation,
        run: AgentRun,
        binding_trace_facts: dict[str, object] | None,
    ) -> None:
        if (
            self.execution_traces is None
            or binding_trace_facts is None
            or not invocation.user.tenant_id
        ):
            return
        turn_id = run.turn_id or formation_turn_id(
            tenant_id=invocation.user.tenant_id,
            user_id=invocation.user.id,
            session_id=invocation.session_id,
            request_id=invocation.request_id or invocation.run_id,
            run_id=invocation.run_id,
        )
        try:
            await self.execution_traces.try_record(
                ExecutionTraceEventDraft(
                    trace_id=trace_id_for_turn(turn_id),
                    tenant_id=invocation.user.tenant_id,
                    user_id=invocation.user.id,
                    session_id=invocation.session_id,
                    turn_id=turn_id,
                    run_id=run.run_id,
                    event_type="agent_run",
                    stage="binding_resolved",
                    status="running",
                    source="oir:binding_resolution",
                    source_event_id=f"binding-resolution:{run.run_id}",
                    facts={
                        "agent_id": run.agent_id,
                        "invoker_type": run.invoker_type,
                        "delegated": False,
                        **binding_trace_facts,
                    },
                    occurred_at=run.created_at or datetime.now(UTC),
                )
            )
        except Exception:
            return

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
        definition: AgentDefinitionV2,
        invocation: AgentInvocation,
        *,
        deadline: InvocationDeadline | None = None,
    ) -> AgentInvocation:
        if deadline is not None:
            deadline.require_remaining()
        active_plan_lookup = self._active_plan_for_invocation(definition, invocation)
        if deadline is None:
            active_plan = await active_plan_lookup
        else:
            active_plan = await deadline.wait_for(active_plan_lookup)
        if deadline is not None:
            deadline.require_remaining()
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
        try:
            assembly = self.agent_context_service.assemble(
                agent=definition,
                user=invocation.user,
                session_id=invocation.session_id,
                query=query,
                invocation_input=input_values,
                caller_type="agent",
                caller_id=definition.agent_id,
                purpose="agent_execution",
                request_id=invocation.request_id,
                # The assembled Context is part of pre-acceptance validation.
                # Do not create usage evidence until a Run has been accepted.
                run_id=None,
                turn_id=None,
                active_plan=active_plan,
                knowledge_context_handle=invocation.knowledge_context_handle,
                knowledge_context_trace_id=invocation.knowledge_context_trace_id,
                deadline_at=deadline.deadline_at if deadline is not None else None,
            )
            if deadline is None:
                runtime = await assembly
            else:
                runtime = await deadline.wait_for(assembly)
        except KnowledgeRequirementError as exc:
            # A known-empty required Context is a client-visible acceptance
            # rejection.  An unavailable provider/controlled handle remains a
            # stable dependency failure, but still occurs before Run/Plan
            # acceptance and never exposes the provider's details.
            if exc.code == "knowledge_not_found":
                raise InvocationPreflightRejectedError(
                    "invocation_required_context_missing"
                ) from None
            raise
        return invocation.model_copy(
            update={
                "input": input_values,
                "memory_context": runtime.memory_context,
                "knowledge_context": runtime.knowledge_context,
                "knowledge_context_handle": None,
                "knowledge_context_trace_id": None,
            }
        )

    async def _record_agent_context_recall_usage(self, invocation: AgentInvocation) -> None:
        """Write Memory usage evidence only after the canonical Run exists."""

        if self.agent_context_service is None or self.memory_service is None:
            return
        record_recall_usage = getattr(self.memory_service, "record_recall_usage", None)
        if record_recall_usage is None or not invocation.user.tenant_id:
            return
        await record_recall_usage(
            invocation.memory_context.items,
            user_id=invocation.user.id,
            tenant_id=invocation.user.tenant_id,
            agent_id=invocation.agent_id,
            consumer=f"agent:{invocation.agent_id}",
            request_id=invocation.request_id,
            session_id=invocation.session_id,
            turn_id=formation_turn_id(
                tenant_id=invocation.user.tenant_id,
                user_id=invocation.user.id,
                session_id=invocation.session_id,
                request_id=invocation.request_id or invocation.run_id,
                run_id=invocation.run_id,
            ),
            run_id=invocation.run_id,
        )

    async def _active_plan_for_invocation(
        self,
        definition: AgentDefinitionV2,
        invocation: AgentInvocation,
    ):
        """Read and validate a Plan without changing its execution state."""

        plan_id = _context_str(invocation.context, "plan_id")
        if not plan_id:
            return None
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
        return candidate

    async def _claim_plan_execution(
        self,
        definition: AgentDefinitionV2,
        invocation: AgentInvocation,
        *,
        deadline: InvocationDeadline,
    ) -> tuple[AgentInvocation, _PlanClaimReservation | None]:
        """Claim the current Step only after all pre-acceptance checks pass."""

        deadline.require_remaining()
        candidate_lookup = self._active_plan_for_invocation(definition, invocation)
        candidate = await deadline.wait_for(candidate_lookup)
        if candidate is None:
            return invocation, None
        step = _current_plan_step(candidate)
        if step is None:
            raise InvocationError("Plan has no executable Step")
        fenced_claim = await deadline.wait_for(
            self.plan_service.get_execution_claim_fence(
                candidate.plan_id,
                tenant_id=candidate.tenant_id,
                user_id=candidate.user_id,
            )
        )
        if fenced_claim is not None:
            # A process may have stopped after persisting the non-expiring
            # Claim but before receiving the Run-start acknowledgement.  The
            # immutable execution key is the durable recovery record: it
            # deterministically identifies the only Run that may exist. Do
            # not mint another Claim/Run until that identity is readable.
            if fenced_claim.step_id != step.step_id:
                raise InvocationError("Plan step is already executing")
            recovered_run_id = _plan_execution_run_id(fenced_claim.execution_key)
            try:
                existing_run = await deadline.wait_for(
                    self.run_repository.get_run(recovered_run_id)
                )
            except InvocationDeadlineExceededError:
                raise
            except Exception:
                raise InvocationBindingUnavailableError(
                    "Plan start recovery is unavailable",
                    details={"reason_code": "plan_start_recovery_unavailable"},
                ) from None
            if existing_run is not None:
                # A fenced Run can have committed just before its worker
                # crashed, including after an Adapter dispatch but before the
                # Plan transition. Converge that one durable Run to a safe
                # terminal state; a new request must never launch another.
                recovered = await self._recover_fenced_existing_run(
                    definition=definition,
                    invocation=invocation,
                    candidate=candidate,
                    step_id=step.step_id,
                    claim_id=fenced_claim.claim_id,
                    run=existing_run,
                    deadline=deadline,
                )
                raise _FencedPlanRunRecovered(recovered)
            reservation = _PlanClaimReservation(
                plan_before_claim=candidate,
                step_id=step.step_id,
                claim_id=fenced_claim.claim_id,
                invocation=invocation,
                fenced_recovery=True,
            )
            return (
                invocation.model_copy(
                    update={
                        "run_id": recovered_run_id,
                        "context": {
                            **invocation.context,
                            "plan_id": candidate.plan_id,
                            "step_id": step.step_id,
                            "plan_status": candidate.status,
                            "plan_execution_claim_id": fenced_claim.claim_id,
                            "plan_execution_idempotency_key": fenced_claim.execution_key,
                        },
                    }
                ),
                reservation,
            )
        requested_claim_id = f"plan_claim_{uuid4().hex}"
        claim_operation = self.plan_service.claim_step(
            candidate.plan_id,
            step.step_id,
            tenant_id=candidate.tenant_id,
            user_id=candidate.user_id,
            claim_id=requested_claim_id,
            lease_seconds=self.plan_claim_lease_seconds,
            publish=not request_prohibits_memory(invocation),
        )
        claim = await deadline.wait_for(
            claim_operation,
            on_late_task=lambda task: self._release_late_unaccepted_plan_claim(
                task,
                plan_before_claim=candidate,
                step_id=step.step_id,
                claim_id=requested_claim_id,
                invocation=invocation,
            ),
        )
        if claim is None:
            raise InvocationError("Plan step is already executing")
        active_plan, claim_id = claim
        if claim_id != requested_claim_id:
            await self._release_unaccepted_plan_claim(
                _PlanClaimReservation(
                    plan_before_claim=candidate,
                    step_id=step.step_id,
                    claim_id=claim_id,
                    invocation=invocation,
                )
            )
            raise InvocationError("Plan execution claim identity is unavailable")
        reservation = _PlanClaimReservation(
            plan_before_claim=candidate,
            step_id=step.step_id,
            claim_id=claim_id,
            invocation=invocation,
        )
        try:
            key_lookup = self.plan_service.get_execution_claim_key(
                active_plan.plan_id,
                claim_id=claim_id,
            )
            execution_key = await deadline.wait_for(key_lookup)
        except InvocationDeadlineExceededError:
            self._defer_unaccepted_plan_claim_release(reservation, deadline=deadline)
            raise
        except asyncio.CancelledError:
            self._defer_unaccepted_plan_claim_release(reservation, deadline=deadline)
            raise
        except Exception:
            await self._release_unaccepted_plan_claim(reservation)
            raise
        if execution_key is None:
            await self._release_unaccepted_plan_claim(reservation)
            raise InvocationError("Plan execution claim is unavailable")
        return (
            invocation.model_copy(
                update={
                    "run_id": _plan_execution_run_id(execution_key),
                    "context": {
                        **invocation.context,
                        "plan_id": active_plan.plan_id,
                        "step_id": step.step_id,
                        "plan_status": active_plan.status,
                        "plan_execution_claim_id": claim_id,
                        "plan_execution_idempotency_key": execution_key,
                    },
                }
            ),
            reservation,
        )

    async def _recover_fenced_existing_run(
        self,
        *,
        definition: AgentDefinitionV2,
        invocation: AgentInvocation,
        candidate: Plan,
        step_id: str,
        claim_id: str,
        run: AgentRun,
        deadline: InvocationDeadline,
    ) -> AgentInvocationResult:
        """Finish a restarted fenced Run without reviving its Adapter call."""

        recovered_invocation = invocation.model_copy(
            update={
                "run_id": run.run_id,
                "input": run.input,
                "context": {
                    **invocation.context,
                    "plan_id": candidate.plan_id,
                    "step_id": step_id,
                    "plan_status": candidate.status,
                    "plan_execution_claim_id": claim_id,
                },
            }
        )
        formation_suppressed = run.formation_suppressed
        if run.status == "running":
            if not deadline.has_elapsed(run.deadline_at):
                # A different request may observe a durable fenced Run while
                # its original worker is still legitimately dispatching. A
                # readable, unexpired Run is evidence of that owner, not of
                # a restart; reject the retry without terminalizing or
                # redispatching it. Once its own persisted deadline passes,
                # a recovery request can converge the orphan safely.
                raise InvocationError("Plan step is already executing")
            result = self.invocation_runtime.project_exception(
                invocation=recovered_invocation,
                agent_id=definition.agent_id,
                # A durable Run whose own persisted deadline elapsed is an
                # accepted call with unknown remote completion, not a new
                # transport failure. Restart recovery must preserve the same
                # deadline terminal vocabulary as the live path.
                exc=InvocationDeadlineExceededError(),
                completion_certainty="unknown",
            )
            return await self._await_terminal_completion(
                self._complete_accepted_invocation(
                    run=run,
                    result=result,
                    invocation=recovered_invocation,
                    started=time.perf_counter(),
                    request_suppressed=formation_suppressed,
                    canonical_managed=bool(run.turn_id),
                    formation_suppressed=formation_suppressed,
                    deadline=deadline,
                )
            )
        if run.status not in {"completed", "failed", "invalid_output", "blocked", "clarify"}:
            raise InvocationError("Plan step is already executing")
        error = ErrorDetail.model_validate(run.error) if run.error else None
        result = AgentInvocationResult(
            run_id=run.run_id,
            agent_id=definition.agent_id,
            status=run.status,
            output=run.output,
            error=error,
        )
        await self._finish_plan_step(
            recovered_invocation,
            result,
            suppress_formation=formation_suppressed,
        )
        return result

    async def _release_unaccepted_plan_claim(
        self,
        reservation: _PlanClaimReservation,
    ) -> None:
        """Revert a claim that cannot cross the Run acceptance boundary.

        Claim persistence increments the Plan version and changes its current
        Step.  Restoring the pre-claim snapshot with the next monotonic
        version lets the existing conditional ``save_claimed_step`` operation
        release exactly this claim without overwriting a concurrent owner.
        """

        plan_service = self.plan_service
        if plan_service is None or reservation.fenced_recovery:
            return
        plan_before_claim = reservation.plan_before_claim
        if plan_before_claim is None:
            return
        restored = plan_before_claim.model_copy(
            update={
                "state_version": plan_before_claim.state_version + 2,
                "updated_at": datetime.now(UTC),
                "formation_event_type": "update",
            },
            deep=True,
        )
        try:
            await plan_service.save_claimed_step(
                restored,
                reservation.step_id,
                claim_id=reservation.claim_id,
                publish=not request_prohibits_memory(reservation.invocation),
            )
        except Exception:
            return

    async def _fence_plan_claim_before_durable_start(
        self,
        reservation: _PlanClaimReservation | None,
    ) -> bool:
        """Persist a non-expiring Claim immediately before a Run start attempt."""

        if reservation is None:
            return True
        plan_service = self.plan_service
        plan_before_claim = reservation.plan_before_claim
        if plan_service is None or plan_before_claim is None:
            return False
        return await plan_service.fence_step_claim(
            plan_before_claim.plan_id,
            reservation.step_id,
            tenant_id=plan_before_claim.tenant_id,
            user_id=plan_before_claim.user_id,
            claim_id=reservation.claim_id,
        )

    def _defer_unaccepted_plan_claim_release(
        self,
        reservation: _PlanClaimReservation | None,
        *,
        deadline: InvocationDeadline,
    ) -> None:
        """Schedule pre-acceptance Claim compensation without delaying a 504."""

        if reservation is not None:
            deadline.observe_late_cleanup(self._release_unaccepted_plan_claim(reservation))

    def _release_late_unaccepted_plan_claim(
        self,
        task: asyncio.Future[object],
        *,
        plan_before_claim: Plan,
        step_id: str,
        claim_id: str,
        invocation: AgentInvocation,
    ) -> Awaitable[None]:
        """Build deferred compensation after a cancelled claim eventually commits."""

        async def release() -> None:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                # Claim persistence may have committed before its post-commit
                # publication was cancelled. The requested opaque claim ID is
                # known before that await, so conditional compensation remains
                # safe even though this task cannot return its tuple.
                pass
            await self._release_unaccepted_plan_claim(
                _PlanClaimReservation(
                    plan_before_claim=plan_before_claim,
                    step_id=step_id,
                    claim_id=claim_id,
                    invocation=invocation,
                )
            )

        return release()

    async def _invoke_with_claim_heartbeat(
        self,
        execute: Callable[[AgentInvocation], Awaitable[AgentInvocationResult]],
        invocation: AgentInvocation,
        *,
        deadline: InvocationDeadline,
    ) -> AgentInvocationResult:
        plan_id = _context_str(invocation.context, "plan_id")
        claim_id = _context_str(invocation.context, "plan_execution_claim_id")
        tenant_id = invocation.user.tenant_id
        if not plan_id or not claim_id or not tenant_id or self.plan_service is None:
            return await execute(invocation)
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
            return await execute(invocation)
        finally:
            stopped.set()
            # A deployment renewal can block while an Adapter has already
            # returned its deadline failure.  Do not let this best-effort
            # lease heartbeat hold the accepted response hostage: retain the
            # real task for lifecycle drain when only a cancelled shield hits
            # the absolute deadline.
            try:
                await deadline.wait_for(
                    asyncio.shield(task),
                    on_late_task=lambda _task: self._retain_claim_heartbeat(task),
                )
            except InvocationDeadlineExceededError:
                pass

    def _retain_claim_heartbeat(self, task: asyncio.Future[object]) -> None:
        """Keep a blocked Plan-claim renewal owned after response timeout."""

        self.invocation_runtime.retain_accepted_execution(task)

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


def _validate_output(
    definition: AgentDefinitionV2,
    result: AgentInvocationResult,
) -> AgentInvocationResult:
    schema = definition.output_schema.model_dump(exclude_none=True)
    if result.output is None or not schema.get("properties"):
        return result
    try:
        validate_json_schema(instance=result.output, schema=schema)
    except JsonSchemaValidationError:
        return result.model_copy(
            update={
                "status": "invalid_output",
                "error": ErrorDetail(
                    code="invalid_output",
                    message="Agent output does not match output schema.",
                ),
            }
        )
    return result


def build_invocation_input(
    definition: AgentDefinitionV2,
    text: str | None = None,
    values: dict | None = None,
) -> dict:
    input_values = dict(values or {})
    if text and "text" in definition.input_schema.required and not input_values.get("text"):
        input_values["text"] = text
    if text:
        for key in definition.input_schema.properties:
            if key not in input_values and key in {"query", "title"}:
                input_values[key] = text
    return input_values


def missing_required_inputs(
    definition: AgentDefinitionV2,
    invocation_input: dict,
) -> list[str]:
    return [item for item in definition.input_schema.required if not invocation_input.get(item)]


def _v2_direct_invocation_context(context: Mapping[str, object]) -> dict[str, object]:
    """Keep v2 Direct Invoke out of Canonical Turn and Plan execution flows."""

    return {
        key: value
        for key, value in context.items()
        if key not in _DIRECT_INVOKE_RESERVED_CONTEXT_KEYS and not key.startswith("_canonical_")
    }


def _context_str(context: dict, key: str) -> str | None:
    value = context.get(key)
    return str(value) if value is not None else None


def _plan_execution_run_id(execution_key: str) -> str:
    """Derive the only durable Run identity for one claimed Plan attempt.

    The key is already a canonical hash of ``plan_id``, ``step_id`` and
    attempt. Reusing its bounded safe representation lets a restarted worker
    prove absence/presence before it retries a fenced start; random Run IDs
    cannot provide that commit-unknown recovery guarantee.
    """

    return f"run_{execution_key}"


def _is_same_durable_start_attempt(existing: AgentRun, attempted: AgentRun) -> bool:
    """Tell an ACK-loss replay from a stable-ID collision in another Turn.

    ``turn_id`` is intentionally excluded: canonical ``start_run`` attaches
    it atomically, so the object retained before the write has ``None``. The
    trusted request/owner/target facts are immutable before and after that
    attachment and distinguish a legitimate acknowledgement loss from a
    competing Turn trying the same fenced Plan execution key.
    """

    return existing.run_id == attempted.run_id and all(
        getattr(existing, field) == getattr(attempted, field)
        for field in (
            "request_id",
            "session_id",
            "agent_id",
            "user_id",
            "tenant_id",
            "plan_id",
            "step_id",
        )
    )


def _run_event_type(status: str) -> str:
    if status == "completed":
        return "complete"
    if status == "cancelled":
        return "cancel"
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
    if status == "cancelled":
        return "cancelled"
    if status in {"blocked", "clarify"}:
        return "blocked"
    return "failed"


def _cancelled_invocation_result(
    *,
    invocation: AgentInvocation,
    agent_id: str,
) -> AgentInvocationResult:
    """Project the closed terminal result after Runtime proves a stop."""

    return AgentInvocationResult(
        run_id=invocation.run_id,
        agent_id=agent_id,
        status="cancelled",
        message="Agent invocation was cancelled.",
    )


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


def _result_from_terminal_run(run: AgentRun) -> AgentInvocationResult:
    """Project an already durable terminal Run without reconstructing Result text.

    This is used only by restart/acknowledgement-loss convergence, where a
    terminal Run proves that another transaction already chose the outcome.
    The Plan transition needs its status and safe error/output facts, not a
    second Result insert or an Adapter replay.
    """

    return AgentInvocationResult(
        run_id=run.run_id,
        agent_id=run.agent_id,
        status=run.status,
        output=run.output,
        error=ErrorDetail.model_validate(run.error) if run.error else None,
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
