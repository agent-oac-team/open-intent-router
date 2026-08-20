from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.core.errors import (
    AgentUnavailableError,
    InvocationBindingUnavailableError,
    InvocationError,
    PlanBindingUnavailableError,
)
from app.schemas.agents import (
    AgentDefinition,
    AgentDefinitionV2,
    ExternalExecutionHandling,
    InvocationHandling,
    UiHandoffHandling,
)
from app.schemas.common import JsonDict, UserContext
from app.schemas.invocation import AgentInvocationResult
from app.schemas.plans import NextAction, Plan, PlanExecutionResponse, PlanStep
from app.services.invocation_service import (
    InvocationService,
    build_invocation_input,
    missing_required_inputs,
)
from app.services.plan_bindings import (
    revalidate_plan_step_binding_against_snapshot,
    validate_frozen_plan_step_binding,
)
from app.services.plan_service import PlanService, PlanStateConflict
from app.services.registry_service import AgentRegistryService
from app.services.registry_snapshot import (
    RegistrySnapshot,
    RegistrySnapshotRuntime,
    RegistrySnapshotSelection,
)

TERMINAL_STATUSES = {"completed", "failed", "blocked", "cancelled"}
PlanAgentDefinition = AgentDefinition | AgentDefinitionV2


@dataclass(frozen=True)
class PlanExecutionCandidateSet:
    """One request-scoped, trusted Candidate Set for Plan preflight and execution."""

    definitions: Mapping[str, PlanAgentDefinition]
    bindings: Mapping[str, RegistrySnapshotSelection]
    legacy_definitions: Mapping[str, AgentDefinition]


def _empty_candidate_set() -> PlanExecutionCandidateSet:
    return PlanExecutionCandidateSet(
        definitions=MappingProxyType({}),
        bindings=MappingProxyType({}),
        legacy_definitions=MappingProxyType({}),
    )


class PlanExecutor:
    def __init__(
        self,
        *,
        plan_service: PlanService,
        registry: AgentRegistryService,
        invocation_service: InvocationService,
        snapshot_runtime: RegistrySnapshotRuntime | None = None,
    ) -> None:
        self.plan_service = plan_service
        self.registry = registry
        self.invocation_service = invocation_service
        self.snapshot_runtime = snapshot_runtime or getattr(
            invocation_service, "snapshot_runtime", None
        )
        if (
            isinstance(self.invocation_service, InvocationService)
            and self.invocation_service.plan_service is None
        ):
            self.invocation_service.plan_service = plan_service

    async def preflight(
        self,
        plan_id: str,
        *,
        user: UserContext,
    ) -> dict[str, PlanAgentDefinition]:
        candidate_set = await self.preflight_candidate_set(plan_id, user=user)
        return dict(candidate_set.definitions)

    async def preflight_candidate_set(
        self,
        plan_id: str,
        *,
        user: UserContext,
    ) -> PlanExecutionCandidateSet:
        """Form a single Candidate Set that a caller may immediately execute."""

        user = _trusted_plan_user(user)
        plan = await self.plan_service.get_plan(
            plan_id,
            tenant_id=user.tenant_id or "",
            user_id=user.id,
        )
        if plan is None:
            raise ValueError("Plan not found")
        if plan.status == "completed":
            if not _all_steps_completed(plan.steps):
                raise PlanStateConflict("Plan is marked completed before every Step completed")
            return _empty_candidate_set()
        if plan.status in {"failed", "cancelled"}:
            return _empty_candidate_set()
        selections: dict[str, RegistrySnapshotSelection] = {}
        frozen_definitions: dict[str, AgentDefinitionV2] = {}
        legacy_definitions: dict[str, AgentDefinition] = {}
        if _plan_has_frozen_bindings(plan):
            snapshot_runtime = self.snapshot_runtime
            captured_snapshot = snapshot_runtime.snapshot if snapshot_runtime is not None else None
            if captured_snapshot is None:
                _raise_plan_snapshot_unavailable()
            selections = self._revalidated_snapshot_selections(
                plan,
                user=user,
                snapshot=captured_snapshot,
            )
            self._preflight_invocation_bindings(selections)
            frozen_definitions.update(
                {agent_id: selection.definition for agent_id, selection in selections.items()}
            )
        if _plan_has_legacy_steps(plan):
            definitions = await self.registry.available_definitions(user)
            legacy_definitions = {definition.agent_id: definition for definition in definitions}
        # Check v2 and legacy Steps independently.  A transition deployment
        # can legitimately expose the same logical id in both registries; a
        # v2 candidate must never make a legacy Step appear available.
        _ensure_plan_agents_available(
            plan,
            frozen_definitions,
            has_frozen_binding=True,
        )
        _ensure_plan_agents_available(
            plan,
            legacy_definitions,
            has_frozen_binding=False,
        )
        # The mapping is a short-lived optimization passed to execute().  The
        # executor keeps the two types separate before dispatching a Step.
        selected: dict[str, PlanAgentDefinition] = dict(frozen_definitions)
        selected.update(legacy_definitions)
        return PlanExecutionCandidateSet(
            definitions=MappingProxyType(selected),
            bindings=MappingProxyType(selections),
            legacy_definitions=MappingProxyType(legacy_definitions),
        )

    async def execute(
        self,
        plan_id: str,
        *,
        user: UserContext,
        input_values: JsonDict | None = None,
        context: JsonDict | None = None,
        max_steps: int = 10,
        selected_definitions: Mapping[str, PlanAgentDefinition] | None = None,
        selected_bindings: Mapping[str, RegistrySnapshotSelection] | None = None,
        selected_legacy_definitions: Mapping[str, AgentDefinition] | None = None,
    ) -> PlanExecutionResponse:
        user = _trusted_plan_user(user)
        tenant_id = user.tenant_id or ""
        plan = await self.plan_service.get_plan(plan_id, tenant_id=tenant_id, user_id=user.id)
        if plan is None:
            raise ValueError("Plan not found")
        if plan.status == "completed":
            if not _all_steps_completed(plan.steps):
                raise PlanStateConflict("Plan is marked completed before every Step completed")
            return PlanExecutionResponse(plan=plan, results=[], next_action=plan.next_action)
        if plan.status in {"failed", "cancelled"}:
            return PlanExecutionResponse(plan=plan, results=[], next_action=plan.next_action)
        routed_definitions = selected_definitions
        snapshot_runtime = self.snapshot_runtime
        captured_snapshot: RegistrySnapshot | None = None
        current_bindings: dict[str, RegistrySnapshotSelection] = {}
        frozen_definitions: dict[str, AgentDefinitionV2] = {}
        legacy_definitions: dict[str, PlanAgentDefinition] = {}
        if _plan_has_frozen_bindings(plan):
            # Route-and-Execute supplies the private Selection objects created
            # by RouterService for this same request.  Reuse that exact
            # Candidate Set rather than observing a Snapshot reload between
            # route and execution.  Other Plan requests form one new Snapshot
            # Candidate Set below.
            current_bindings = _trusted_route_bindings_for_plan(
                plan,
                user=user,
                selected_bindings=selected_bindings,
            )
            if current_bindings is None:
                captured_snapshot = (
                    snapshot_runtime.snapshot if snapshot_runtime is not None else None
                )
                if captured_snapshot is None:
                    _raise_plan_snapshot_unavailable()
                current_bindings = self._revalidated_snapshot_selections(
                    plan,
                    user=user,
                    snapshot=captured_snapshot,
                )
            self._preflight_invocation_bindings(current_bindings)
            frozen_definitions.update(
                {agent_id: selection.definition for agent_id, selection in current_bindings.items()}
            )
        if _plan_has_legacy_steps(plan):
            if selected_legacy_definitions is not None:
                routed_legacy_definitions = selected_legacy_definitions.values()
            else:
                routed_legacy_definitions = (
                    routed_definitions.values()
                    if routed_definitions is not None
                    else await self.registry.available_definitions(user)
                )
            # A Route-and-Execute request supplies its private, trusted
            # selection here.  Delayed execution has no such selection and
            # re-forms the legacy Candidate Set above.  Keep this map
            # separate from frozen v2 selections so a registry collision
            # cannot silently switch a legacy Step's handling.
            legacy_definitions = {
                definition.agent_id: definition for definition in routed_legacy_definitions
            }
        _ensure_plan_agents_available(
            plan,
            frozen_definitions,
            has_frozen_binding=True,
        )
        _ensure_plan_agents_available(
            plan,
            legacy_definitions,
            has_frozen_binding=False,
        )
        results: list[JsonDict] = []
        next_action = plan.next_action
        execution_context = dict(context or {})
        publish_plan = not plan_execution_prohibits_memory(input_values, context)
        previous_results = list(execution_context.get("previous_results") or [])

        for _ in range(max_steps):
            reloaded = await self.plan_service.get_plan(
                plan_id,
                tenant_id=tenant_id,
                user_id=user.id,
            )
            if reloaded is None:
                raise ValueError("Plan not found")
            plan = reloaded
            if plan.status == "completed":
                if not _all_steps_completed(plan.steps):
                    raise PlanStateConflict("Plan is marked completed before every Step completed")
                return PlanExecutionResponse(
                    plan=plan, results=results, next_action=plan.next_action
                )
            if plan.status in {"failed", "cancelled"}:
                return PlanExecutionResponse(
                    plan=plan, results=results, next_action=plan.next_action
                )
            if any(step.status == "failed" for step in plan.steps):
                plan = await self.plan_service.save_plan(
                    plan.model_copy(
                        update={"status": "failed", "current_step_id": None, "next_action": None}
                    ),
                    publish=publish_plan,
                )
                return PlanExecutionResponse(plan=plan, results=results)
            if _all_steps_completed(plan.steps):
                plan = await self.plan_service.save_plan(
                    plan.model_copy(
                        update={"status": "completed", "current_step_id": None, "next_action": None}
                    ),
                    publish=publish_plan,
                )
                return PlanExecutionResponse(plan=plan, results=results)

            resuming = bool(input_values or context)
            if plan.status == "blocked" and not resuming:
                return PlanExecutionResponse(
                    plan=plan, results=results, next_action=plan.next_action
                )
            step = _select_executable_step(plan, resuming=resuming)
            if step is None:
                if any(item.status == "blocked" for item in plan.steps):
                    return PlanExecutionResponse(
                        plan=plan, results=results, next_action=plan.next_action
                    )
                raise PlanStateConflict("Plan has incomplete Steps but no ready Step")

            # Re-resolve and compare before a pending Step is marked running or
            # InvocationService can claim it.  A delayed Plan must not mutate
            # its execution state when the current Principal/Binding no longer
            # satisfies its frozen declaration.
            selection: RegistrySnapshotSelection | None = None
            if _step_has_frozen_binding(step):
                if captured_snapshot is None:
                    selection = current_bindings.get(step.agent_id)
                    if selection is None:
                        raise PlanBindingUnavailableError(
                            "Plan Binding is unavailable",
                            details={"reason_code": "plan_binding_unavailable"},
                        )
                    # The private Route capability was validated against the
                    # current Principal before execute() began.  Re-compare in
                    # case another worker changed the stored Step meanwhile.
                    validate_frozen_plan_step_binding(step, selection)
                else:
                    selection = revalidate_plan_step_binding_against_snapshot(
                        step,
                        snapshot=captured_snapshot,
                        user=user,
                    )
                current_bindings[step.agent_id] = selection
                definition = selection.definition
            else:
                definition = legacy_definitions[step.agent_id]
            resolved_binding = None
            if isinstance(definition, AgentDefinitionV2) and isinstance(
                definition.handling, InvocationHandling
            ):
                if selection is None:  # pragma: no cover - frozen v2 steps always select first
                    _raise_plan_snapshot_unavailable()
                resolved_binding = self.invocation_service.resolve_direct_binding(selection)
            if step.status == "pending" and (
                plan.current_step_id != step.step_id
                or plan.status != "running"
                or plan.next_action is not None
            ):
                plan = await self.plan_service.save_plan(
                    plan.model_copy(
                        update={
                            "current_step_id": step.step_id,
                            "status": "running",
                            "next_action": None,
                        }
                    ),
                    publish=publish_plan,
                )

            if isinstance(definition, AgentDefinitionV2):
                if isinstance(definition.handling, UiHandoffHandling):
                    next_action = NextAction(
                        type="open_ui",
                        message="需要宿主应用打开对应界面继续执行。",
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        agent_id=definition.agent_id,
                        route=definition.handling.route,
                        params=definition.handling.params.model_dump(
                            mode="json", exclude_none=True
                        ),
                        metadata={"handling_kind": "ui_handoff"},
                    )
                    plan = await self._save_step_status(
                        plan, step, "blocked", next_action, publish=publish_plan
                    )
                    return PlanExecutionResponse(
                        plan=plan, results=results, next_action=next_action
                    )
                if isinstance(definition.handling, ExternalExecutionHandling):
                    next_action = NextAction(
                        type="wait_for_agent_event",
                        message="该步骤由宿主外部执行，等待 Agent 返回结果。",
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        agent_id=definition.agent_id,
                        metadata={"handling_kind": "external_execution"},
                    )
                    plan = await self._save_step_status(
                        plan, step, "blocked", next_action, publish=publish_plan
                    )
                    return PlanExecutionResponse(
                        plan=plan, results=results, next_action=next_action
                    )
                if selection is None:
                    raise InvocationBindingUnavailableError(
                        "Invocation Binding is unavailable",
                        details={"reason_code": "plan_binding_unavailable"},
                    )
            else:
                if definition.type == "ui_handoff":
                    next_action = NextAction(
                        type="open_ui",
                        message="需要宿主应用打开对应界面继续执行。",
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        agent_id=definition.agent_id,
                        route=definition.ui_handoff.route,
                        params=definition.ui_handoff.params,
                    )
                    plan = await self._save_step_status(
                        plan, step, "blocked", next_action, publish=publish_plan
                    )
                    return PlanExecutionResponse(
                        plan=plan, results=results, next_action=next_action
                    )

                if not self.invocation_service.invokers.has(definition.type):
                    next_action = NextAction(
                        type="wait_for_agent_event",
                        message="该步骤需要外部 Agent Runtime 或宿主应用继续执行。",
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        agent_id=definition.agent_id,
                    )
                    plan = await self._save_step_status(
                        plan, step, "blocked", next_action, publish=publish_plan
                    )
                    return PlanExecutionResponse(
                        plan=plan, results=results, next_action=next_action
                    )

            invocation_input = build_invocation_input(
                definition,
                values=_merge_step_inputs(input_values or {}, previous_results),
            )
            missing = missing_required_inputs(definition, invocation_input)
            if missing:
                next_action = NextAction(
                    type="collect_input",
                    message=f"缺少必要输入：{', '.join(missing)}。",
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    agent_id=definition.agent_id,
                    metadata={"missing_inputs": missing},
                )
                plan = await self._save_step_status(
                    plan, step, "blocked", next_action, publish=publish_plan
                )
                return PlanExecutionResponse(plan=plan, results=results, next_action=next_action)

            try:
                result = await self.invocation_service.invoke_agent(
                    agent_id=definition.agent_id,
                    session_id=plan.session_id or "",
                    user=user,
                    input=invocation_input,
                    context={
                        **execution_context,
                        "plan_id": plan.plan_id,
                        "step_id": step.step_id,
                        "previous_results": previous_results,
                    },
                    selected_definition=definition,
                    selected_binding=selection,
                    resolved_binding=resolved_binding,
                )
            except InvocationError as exc:
                if exc.message != "Plan step is already executing":
                    raise
                current = await self.plan_service.get_plan(
                    plan_id,
                    tenant_id=tenant_id,
                    user_id=user.id,
                )
                if current is None:
                    raise ValueError("Plan not found") from exc
                return PlanExecutionResponse(
                    plan=current,
                    results=results,
                    next_action=current.next_action,
                )
            result_payload = _result_payload(step, result)
            results.append(result_payload)
            previous_results.append(result_payload)
            execution_context["previous_results"] = previous_results
            next_status = _result_to_step_status(result)
            reloaded = await self.plan_service.get_plan(
                plan_id,
                tenant_id=tenant_id,
                user_id=user.id,
            )
            if reloaded is None:
                raise ValueError("Plan not found")
            plan = reloaded
            if plan.status in TERMINAL_STATUSES:
                return PlanExecutionResponse(
                    plan=plan,
                    results=results,
                    next_action=plan.next_action,
                )
            plan = reloaded
            if next_status != "completed":
                return PlanExecutionResponse(
                    plan=plan, results=results, next_action=plan.next_action
                )

        return PlanExecutionResponse(plan=plan, results=results, next_action=next_action)

    async def _save_step_status(
        self,
        plan: Plan,
        step: PlanStep,
        status: str,
        next_action: NextAction | None,
        *,
        publish: bool,
    ) -> Plan:
        updated = self._updated_step_status(plan, step, status, next_action)
        return await self.plan_service.save_plan(updated, publish=publish)

    def _updated_step_status(
        self,
        plan: Plan,
        step: PlanStep,
        status: str,
        next_action: NextAction | None,
    ) -> Plan:
        updated_steps = [
            item.model_copy(update={"status": status}) if item.step_id == step.step_id else item
            for item in plan.steps
        ]
        current_step_id = _next_step_id(updated_steps)
        plan_status = _plan_status(updated_steps, current_step_id, next_action)
        return plan.model_copy(
            update={
                "steps": updated_steps,
                "current_step_id": current_step_id,
                "status": plan_status,
                "next_action": next_action,
            }
        )

    def _revalidated_snapshot_selections(
        self,
        plan: Plan,
        *,
        user: UserContext,
        snapshot: RegistrySnapshot,
    ) -> dict[str, RegistrySnapshotSelection]:
        selections: dict[str, RegistrySnapshotSelection] = {}
        for step in plan.steps:
            if step.status in {"completed", "failed", "cancelled"} or not _step_has_frozen_binding(
                step
            ):
                continue
            selection = revalidate_plan_step_binding_against_snapshot(
                step,
                snapshot=snapshot,
                user=user,
            )
            selections[step.agent_id] = selection
        return selections

    def _preflight_invocation_bindings(
        self,
        selections: Mapping[str, RegistrySnapshotSelection],
    ) -> None:
        """Reject broken Adapter protocols before a Plan Step becomes running."""

        for selection in selections.values():
            if isinstance(selection.definition.handling, InvocationHandling):
                self.invocation_service.resolve_direct_binding(selection)


def _select_executable_step(plan: Plan, *, resuming: bool) -> PlanStep | None:
    current = next(
        (step for step in plan.steps if step.step_id == plan.current_step_id),
        None,
    )
    if (
        current is not None
        and current.status == "running"
        and _dependencies_complete(current, plan.steps)
    ):
        return current
    if (
        resuming
        and current is not None
        and current.status == "blocked"
        and _dependencies_complete(current, plan.steps)
    ):
        return current
    ready = _ready_steps(plan.steps)
    return ready[0] if ready else None


def _ready_steps(steps: list[PlanStep]) -> list[PlanStep]:
    completed = {step.step_id for step in steps if step.status == "completed"}
    return [
        step
        for step in steps
        if step.status == "pending" and all(parent in completed for parent in step.depends_on)
    ]


def _all_steps_completed(steps: list[PlanStep]) -> bool:
    return all(step.status == "completed" for step in steps)


def _trusted_plan_user(user: UserContext) -> UserContext:
    if not isinstance(user, UserContext):
        user = UserContext.model_validate(user)
    if not user.tenant_id:
        raise ValueError("Trusted tenant identity is required to execute a Plan")
    return user


def _ensure_plan_agents_available(
    plan: Plan,
    selected_definitions: Mapping[str, PlanAgentDefinition],
    *,
    has_frozen_binding: bool | None = None,
) -> None:
    unavailable = [
        step.agent_id
        for step in plan.steps
        if step.status not in {"completed", "failed", "cancelled"}
        and (has_frozen_binding is None or _step_has_frozen_binding(step) is has_frozen_binding)
        and step.agent_id not in selected_definitions
    ]
    if unavailable:
        raise AgentUnavailableError(
            f"Agent is not available: {unavailable[0]}",
            details={"agent_ids": list(dict.fromkeys(unavailable))},
        )


def _trusted_route_bindings_for_plan(
    plan: Plan,
    *,
    user: UserContext,
    selected_bindings: Mapping[str, RegistrySnapshotSelection] | None,
) -> dict[str, RegistrySnapshotSelection] | None:
    """Validate Router's private one-request Snapshot capability.

    The mapping never crosses the HTTP boundary: it is attached by RouterService
    and immediately consumed by route-and-execute.  Still verify every frozen
    Step and one Snapshot id here so a malformed internal handoff fails closed
    instead of accidentally mixing Candidate Sets.
    """

    if selected_bindings is None:
        return None
    selections: dict[str, RegistrySnapshotSelection] = {}
    snapshot_ids: set[str] = set()
    for step in plan.steps:
        if step.status in {"completed", "failed", "cancelled"} or not _step_has_frozen_binding(
            step
        ):
            continue
        selection = selected_bindings.get(step.agent_id)
        if not isinstance(selection, RegistrySnapshotSelection):
            raise PlanBindingUnavailableError(
                "Plan Binding is unavailable",
                details={"reason_code": "plan_binding_unavailable"},
            )
        if not selection.entry.is_executable:
            raise PlanBindingUnavailableError(
                "Plan Binding is unavailable",
                details={"reason_code": "plan_binding_unavailable"},
            )
        definition = selection.definition
        if not definition.access_policy.allows(user):
            raise AgentUnavailableError(
                f"Agent is not available: {step.agent_id}",
                details={"agent_ids": [step.agent_id]},
            )
        validate_frozen_plan_step_binding(step, selection)
        selections[step.agent_id] = selection
        snapshot_ids.add(selection.snapshot_id)
    if len(snapshot_ids) != 1:
        raise PlanBindingUnavailableError(
            "Plan Binding is unavailable",
            details={"reason_code": "plan_binding_unavailable"},
        )
    return selections


def _plan_has_frozen_bindings(plan: Plan) -> bool:
    # Terminal Steps no longer dispatch or resume, so they do not need a live
    # Registry Snapshot merely to return their durable terminal Plan state.
    if plan.status in {"completed", "failed", "cancelled"}:
        return False
    return any(
        step.status not in {"completed", "failed", "cancelled"} and _step_has_frozen_binding(step)
        for step in plan.steps
    )


def _plan_has_legacy_steps(plan: Plan) -> bool:
    if plan.status in {"completed", "failed", "cancelled"}:
        return False
    return any(
        step.status not in {"completed", "failed", "cancelled"}
        and not _step_has_frozen_binding(step)
        for step in plan.steps
    )


def _step_has_frozen_binding(step: PlanStep) -> bool:
    return step.agent_revision is not None or step.binding_requirement is not None


def _raise_plan_snapshot_unavailable() -> None:
    raise PlanBindingUnavailableError(
        "Registry Snapshot is unavailable for Plan execution",
        details={"reason_code": "plan_snapshot_unavailable"},
    )


def _dependencies_complete(step: PlanStep, steps: list[PlanStep]) -> bool:
    completed = {item.step_id for item in steps if item.status == "completed"}
    return all(parent in completed for parent in step.depends_on)


def _next_step_id(steps: list[PlanStep]) -> str | None:
    completed = {step.step_id for step in steps if step.status == "completed"}
    for step in steps:
        if step.status in {"running", "blocked", "failed"}:
            return step.step_id
        if step.status == "pending" and all(parent in completed for parent in step.depends_on):
            return step.step_id
    return None


def _plan_status(
    steps: list[PlanStep], current_step_id: str | None, next_action: NextAction | None
) -> str:
    if any(step.status == "failed" for step in steps):
        return "failed"
    if next_action and next_action.type in {"open_ui", "collect_input", "wait_for_agent_event"}:
        return "blocked"
    if any(step.status == "blocked" for step in steps):
        return "blocked"
    if all(step.status == "completed" for step in steps):
        return "completed"
    if current_step_id:
        return "running"
    return "pending"


def _result_to_step_status(result: AgentInvocationResult) -> str:
    if result.status == "completed":
        return "completed"
    if result.status in {"blocked", "clarify"}:
        return "blocked"
    return "failed"


def _result_payload(step: PlanStep, result: AgentInvocationResult) -> JsonDict:
    return {
        "step_id": step.step_id,
        "agent_id": step.agent_id,
        "run_id": result.run_id,
        "status": result.status,
        "message": result.message,
        "output": result.output,
        "artifact_refs": [ref.model_dump(mode="json") for ref in result.artifact_refs],
        "error": result.error.model_dump(mode="json") if result.error else None,
    }


def _merge_step_inputs(input_values: JsonDict, previous_results: list[JsonDict]) -> JsonDict:
    merged = dict(input_values)
    if previous_results:
        latest_output = previous_results[-1].get("output")
        if isinstance(latest_output, dict):
            for key, value in latest_output.items():
                merged.setdefault(key, value)
                if key == "summary":
                    merged.setdefault("text", value)
                    merged.setdefault("title", value)
                    merged.setdefault("query", value)
    return merged


def plan_execution_prohibits_memory(
    input_values: JsonDict | None,
    context: JsonDict | None,
) -> bool:
    for source in (input_values or {}, context or {}):
        if source.get("temporary") is True or source.get("private") is True:
            return True
        policy = source.get("memory_policy")
        if isinstance(policy, str) and policy.lower() in {"off", "temporary", "private"}:
            return True
        if isinstance(policy, dict):
            mode = str(policy.get("mode", "")).lower()
            if mode in {"off", "temporary", "private"} or policy.get("enabled") is False:
                return True
    return False
