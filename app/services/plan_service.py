from uuid import uuid4

from app.schemas.events import AgentEvent
from app.schemas.plans import Plan, PlanActionResponse


class PlanService:
    def __init__(self, repository) -> None:
        self.repository = repository

    async def save_plan(self, plan: Plan) -> Plan:
        return await self.repository.save(plan)

    async def get_plan(self, plan_id: str) -> Plan | None:
        return await self.repository.get(plan_id)

    async def confirm(self, plan_id: str) -> PlanActionResponse:
        plan = await self.get_plan(plan_id)
        if plan is None:
            raise ValueError("Plan not found")
        updated = plan.model_copy(update={"status": "running"})
        await self.repository.save(updated)
        return PlanActionResponse(
            plan_id=plan_id, status="running", current_step_id=updated.current_step_id
        )

    async def cancel(self, plan_id: str) -> PlanActionResponse:
        plan = await self.get_plan(plan_id)
        if plan is None:
            raise ValueError("Plan not found")
        updated = plan.model_copy(update={"status": "cancelled"})
        await self.repository.save(updated)
        return PlanActionResponse(
            plan_id=plan_id, status="cancelled", current_step_id=updated.current_step_id
        )

    async def apply_agent_event(self, event: AgentEvent) -> Plan | None:
        if not event.plan_id:
            return None
        plan = await self.get_plan(event.plan_id)
        if plan is None:
            return None
        updated_steps = []
        event_status = _event_status_to_step_status(event.status, event.event_type)
        for step in plan.steps:
            if step.step_id != event.step_id:
                updated_steps.append(step)
                continue
            updated_steps.append(step.model_copy(update={"status": event_status}))
        current_step_id = _next_step_id(updated_steps)
        plan_status = _plan_status(updated_steps, current_step_id)
        updated = plan.model_copy(
            update={
                "steps": updated_steps,
                "current_step_id": current_step_id,
                "status": plan_status,
            }
        )
        return await self.repository.save(updated)

    def new_plan_id(self) -> str:
        return f"plan_{uuid4().hex}"


def _event_status_to_step_status(status: str | None, event_type: str) -> str:
    if status in {"pending", "running", "blocked", "completed", "failed", "cancelled"}:
        return status
    if event_type == "agent_error":
        return "failed"
    if event_type == "agent_clarify":
        return "blocked"
    if event_type == "agent_progress":
        return "running"
    return "completed"


def _next_step_id(steps) -> str | None:
    completed = {step.step_id for step in steps if step.status == "completed"}
    for step in steps:
        if step.status == "pending" and all(parent in completed for parent in step.depends_on):
            return step.step_id
        if step.status in {"running", "blocked", "failed"}:
            return step.step_id
    return None


def _plan_status(steps, current_step_id: str | None) -> str:
    if any(step.status == "failed" for step in steps):
        return "failed"
    if any(step.status == "blocked" for step in steps):
        return "blocked"
    if all(step.status == "completed" for step in steps):
        return "completed"
    if current_step_id:
        return "running"
    return "pending"
