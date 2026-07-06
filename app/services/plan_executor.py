from app.schemas.common import JsonDict, UserContext
from app.schemas.invocation import AgentInvocationResult
from app.schemas.plans import NextAction, Plan, PlanExecutionResponse, PlanStep
from app.services.invocation_service import (
    InvocationService,
    build_invocation_input,
    missing_required_inputs,
)
from app.services.plan_service import PlanService
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

    async def execute(
        self,
        plan_id: str,
        *,
        user: UserContext,
        input_values: JsonDict | None = None,
        context: JsonDict | None = None,
        max_steps: int = 10,
    ) -> PlanExecutionResponse:
        plan = await self.plan_service.get_plan(plan_id)
        if plan is None:
            raise ValueError("Plan not found")
        results: list[JsonDict] = []
        next_action = plan.next_action
        execution_context = dict(context or {})
        previous_results = list(execution_context.get("previous_results") or [])

        for _ in range(max_steps):
            if plan.status in TERMINAL_STATUSES and not (
                plan.status == "blocked" and (input_values or context)
            ):
                return PlanExecutionResponse(
                    plan=plan, results=results, next_action=plan.next_action
                )
            step = _current_or_next_step(plan)
            if step is None:
                plan = await self.plan_service.save_plan(
                    plan.model_copy(
                        update={"status": "completed", "current_step_id": None, "next_action": None}
                    )
                )
                return PlanExecutionResponse(plan=plan, results=results)

            definition = await self.registry.get_definition(step.agent_id)
            if definition is None:
                next_action = NextAction(
                    type="collect_input",
                    message=f"Agent not found: {step.agent_id}",
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    agent_id=step.agent_id,
                )
                plan = await self._save_step_status(plan, step, "blocked", next_action)
                return PlanExecutionResponse(plan=plan, results=results, next_action=next_action)

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
                plan = await self._save_step_status(plan, step, "blocked", next_action)
                return PlanExecutionResponse(plan=plan, results=results, next_action=next_action)

            if not self.invocation_service.invokers.has(definition.type):
                next_action = NextAction(
                    type="wait_for_agent_event",
                    message="该步骤需要外部 Agent Runtime 或宿主应用继续执行。",
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    agent_id=definition.agent_id,
                )
                plan = await self._save_step_status(plan, step, "blocked", next_action)
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
                plan = await self._save_step_status(plan, step, "blocked", next_action)
                return PlanExecutionResponse(plan=plan, results=results, next_action=next_action)

            plan = await self._save_step_status(plan, step, "running", None)
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
            )
            result_payload = _result_payload(step, result)
            results.append(result_payload)
            previous_results.append(result_payload)
            execution_context["previous_results"] = previous_results
            next_status = _result_to_step_status(result)
            plan = await self._save_step_status(plan, step, next_status, None)
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
    ) -> Plan:
        updated_steps = [
            item.model_copy(update={"status": status}) if item.step_id == step.step_id else item
            for item in plan.steps
        ]
        current_step_id = _next_step_id(updated_steps)
        plan_status = _plan_status(updated_steps, current_step_id, next_action)
        updated = plan.model_copy(
            update={
                "steps": updated_steps,
                "current_step_id": current_step_id,
                "status": plan_status,
                "next_action": next_action,
            }
        )
        return await self.plan_service.save_plan(updated)


def _current_or_next_step(plan: Plan) -> PlanStep | None:
    if plan.current_step_id:
        for step in plan.steps:
            if step.step_id == plan.current_step_id and step.status in {
                "pending",
                "running",
                "blocked",
            }:
                return step if _dependencies_complete(step, plan.steps) else None
    completed = {step.step_id for step in plan.steps if step.status == "completed"}
    for step in plan.steps:
        if step.status == "pending" and all(parent in completed for parent in step.depends_on):
            return step
    return None


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
