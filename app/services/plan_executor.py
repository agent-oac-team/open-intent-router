from collections.abc import Mapping

from app.core.errors import AgentUnavailableError, InvocationError
from app.schemas.agents import AgentDefinition
from app.schemas.common import JsonDict, UserContext
from app.schemas.invocation import AgentInvocationResult
from app.schemas.plans import NextAction, Plan, PlanExecutionResponse, PlanStep
from app.services.invocation_service import (
    InvocationService,
    build_invocation_input,
    missing_required_inputs,
)
from app.services.plan_service import PlanService, PlanStateConflict
from app.services.registry_service import AgentRegistryService

TERMINAL_STATUSES = {"completed", "failed", "blocked", "cancelled"}


class PlanExecutor:
    def __init__(
        self,
        *,
        plan_service: PlanService,
        registry: AgentRegistryService,
        invocation_service: InvocationService,
    ) -> None:
        self.plan_service = plan_service
        self.registry = registry
        self.invocation_service = invocation_service
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
    ) -> dict[str, AgentDefinition]:
        user = _trusted_plan_user(user)
        plan = await self.plan_service.get_plan(
            plan_id,
            tenant_id=user.tenant_id or "",
            user_id=user.id,
        )
        if plan is None:
            raise ValueError("Plan not found")
        definitions = await self.registry.available_definitions(user)
        selected = {definition.agent_id: definition for definition in definitions}
        _ensure_plan_agents_available(plan, selected)
        return selected

    async def execute(
        self,
        plan_id: str,
        *,
        user: UserContext,
        input_values: JsonDict | None = None,
        context: JsonDict | None = None,
        max_steps: int = 10,
        selected_definitions: Mapping[str, AgentDefinition] | None = None,
    ) -> PlanExecutionResponse:
        user = _trusted_plan_user(user)
        tenant_id = user.tenant_id or ""
        plan = await self.plan_service.get_plan(plan_id, tenant_id=tenant_id, user_id=user.id)
        if plan is None:
            raise ValueError("Plan not found")
        if selected_definitions is None:
            selected_definitions = await self.preflight(plan_id, user=user)
        selected_definitions = dict(selected_definitions)
        _ensure_plan_agents_available(plan, selected_definitions)
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

            definition = selected_definitions[step.agent_id]

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
                return PlanExecutionResponse(plan=plan, results=results, next_action=next_action)

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
                return PlanExecutionResponse(plan=plan, results=results, next_action=next_action)

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
    selected_definitions: Mapping[str, AgentDefinition],
) -> None:
    unavailable = [
        step.agent_id
        for step in plan.steps
        if step.status not in {"completed", "failed", "cancelled"}
        and step.agent_id not in selected_definitions
    ]
    if unavailable:
        raise AgentUnavailableError(
            f"Agent is not available: {unavailable[0]}",
            details={"agent_ids": list(dict.fromkeys(unavailable))},
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
