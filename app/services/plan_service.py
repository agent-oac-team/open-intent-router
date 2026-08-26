from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.memory_runtime import MemoryRuntimePolicy, build_memory_runtime_policy
from app.schemas.events import AgentEvent
from app.schemas.plans import HostManagedStepCompletion, Plan, PlanActionResponse


class PlanStateConflict(RuntimeError):
    pass


class PlanService:
    def __init__(
        self,
        repository,
        *,
        run_repository=None,
        structured_formation=None,
        runtime_policy: MemoryRuntimePolicy | None = None,
    ) -> None:
        self.repository = repository
        self.run_repository = run_repository
        self.structured_formation = structured_formation
        self.runtime_policy = runtime_policy or build_memory_runtime_policy(
            "on" if structured_formation is not None else "off",
            config_source="service_composition",
        )

    @property
    def automatic_formation_enabled(self) -> bool:
        return self.runtime_policy.effective_formation_mode != "off"

    async def save_plan(
        self,
        plan: Plan,
        *,
        event_type: str | None = None,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
        publish: bool = True,
    ) -> Plan:
        existing = await self.repository.get(
            plan.plan_id,
            tenant_id=plan.tenant_id,
            user_id=plan.user_id,
        )
        if existing is not None and plan.state_version != existing.state_version:
            raise PlanStateConflict("Plan input is stale")
        resolved_event_type = event_type or ("update" if existing is not None else "create")
        plan = plan.model_copy(
            update={
                "last_event_id": (
                    event_id
                    if event_id is not None
                    else existing.last_event_id
                    if existing is not None
                    else None
                ),
                "state_version": existing.state_version + 1 if existing is not None else 1,
                "updated_at": occurred_at or datetime.now(UTC),
                "formation_event_type": resolved_event_type,
            }
        )
        if existing is None:
            stored = await self.repository.save(
                plan,
                formation_suppressed=(not publish or not self.automatic_formation_enabled),
            )
        else:
            stored = await self.repository.save_if_version(
                plan,
                expected_version=existing.state_version,
                formation_suppressed=(not publish or not self.automatic_formation_enabled),
            )
            if stored is None:
                raise PlanStateConflict("Plan changed concurrently")
        if publish:
            await self._publish(
                stored,
                event_type=resolved_event_type,
                event_id=event_id,
                occurred_at=occurred_at,
            )
        return stored

    async def get_plan(self, plan_id: str, *, tenant_id: str, user_id: str) -> Plan | None:
        return await self.repository.get(plan_id, tenant_id=tenant_id, user_id=user_id)

    async def get_active_plan(
        self, session_id: str, *, tenant_id: str, user_id: str
    ) -> Plan | None:
        return await self.repository.get_active_by_session(
            session_id, tenant_id=tenant_id, user_id=user_id
        )

    async def claim_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str | None = None,
        lease_seconds: float = 300,
        publish: bool = True,
    ) -> tuple[Plan, str] | None:
        now = datetime.now(UTC)
        claim_id = claim_id or f"plan_claim_{uuid4().hex}"
        plan = await self.repository.claim_step(
            plan_id,
            step_id,
            tenant_id=tenant_id,
            user_id=user_id,
            claim_id=claim_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            now=now,
            formation_suppressed=(not publish or not self.automatic_formation_enabled),
        )
        if plan is None:
            return None
        if publish:
            await self._publish(
                plan,
                event_type="update",
                event_id=None,
                occurred_at=now,
            )
        return plan, claim_id

    async def get_execution_claim_key(self, plan_id: str, *, claim_id: str) -> str | None:
        return await self.repository.get_execution_claim_key(plan_id, claim_id=claim_id)

    async def get_execution_claim_fence(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ):
        """Return a non-expiring start reservation for restart-safe recovery."""

        return await self.repository.get_execution_claim_fence(
            plan_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def fence_step_claim(
        self,
        plan_id: str,
        step_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str,
    ) -> bool:
        """Prevent lease expiry from redispatching while durable Run start is unknown."""

        return await self.repository.fence_step_claim(
            plan_id,
            step_id,
            tenant_id=tenant_id,
            user_id=user_id,
            claim_id=claim_id,
        )

    async def renew_step_claim(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str,
        lease_seconds: float,
    ) -> bool:
        now = datetime.now(UTC)
        return await self.repository.renew_step_claim(
            plan_id,
            tenant_id=tenant_id,
            user_id=user_id,
            claim_id=claim_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            now=now,
        )

    async def save_claimed_step(
        self,
        plan: Plan,
        step_id: str,
        *,
        claim_id: str,
        publish: bool = True,
    ) -> Plan | None:
        plan = plan.model_copy(
            update={"updated_at": datetime.now(UTC), "formation_event_type": "update"}
        )
        stored = await self.repository.save_claimed_step(
            plan,
            step_id,
            claim_id=claim_id,
            formation_suppressed=(not publish or not self.automatic_formation_enabled),
        )
        if stored is not None and publish:
            await self._publish(
                stored,
                event_type="update",
                event_id=None,
                occurred_at=None,
            )
        return stored

    async def finish_claimed_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str,
        status: str,
        publish: bool = True,
    ) -> Plan | None:
        for _ in range(3):
            plan = await self.get_plan(plan_id, tenant_id=tenant_id, user_id=user_id)
            if plan is None or plan.status in {"completed", "failed", "cancelled", "blocked"}:
                return plan
            updated_steps = []
            found = False
            for step in plan.steps:
                if step.step_id != step_id:
                    updated_steps.append(step)
                    continue
                found = True
                updated_steps.append(
                    step.model_copy(update={"status": _advance_step_status(step.status, status)})
                )
            if not found:
                return None
            current_step_id = _next_step_id(updated_steps)
            updated = plan.model_copy(
                update={
                    "steps": updated_steps,
                    "current_step_id": current_step_id,
                    "status": _plan_status(updated_steps, current_step_id),
                    "next_action": None,
                    "state_version": plan.state_version + 1,
                }
            )
            stored = await self.save_claimed_step(
                updated,
                step_id,
                claim_id=claim_id,
                publish=publish,
            )
            if stored is not None:
                return stored
        canonical = await self.get_plan(plan_id, tenant_id=tenant_id, user_id=user_id)
        if canonical is None or canonical.status in {"completed", "failed", "cancelled", "blocked"}:
            return canonical
        raise PlanStateConflict("Plan claim completion conflicted repeatedly")

    async def confirm(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str | None = None,
        expected_state_version: int | None = None,
        publish: bool = True,
    ) -> PlanActionResponse:
        plan = await self.get_plan(plan_id, tenant_id=tenant_id, user_id=user_id)
        if plan is None:
            raise ValueError("Plan not found")
        if request_id is not None and plan.last_event_id == request_id:
            return PlanActionResponse(
                plan_id=plan_id,
                status=plan.status,
                current_step_id=plan.current_step_id,
                next_action=plan.next_action,
                state_version=plan.state_version,
                transitioned=True,
            )
        if expected_state_version is not None and plan.state_version != expected_state_version:
            return PlanActionResponse(
                plan_id=plan_id,
                status=plan.status,
                current_step_id=plan.current_step_id,
                next_action=plan.next_action,
                state_version=plan.state_version,
            )
        if plan.status != "pending":
            return PlanActionResponse(
                plan_id=plan_id,
                status=plan.status,
                current_step_id=plan.current_step_id,
                next_action=plan.next_action,
                state_version=plan.state_version,
            )
        updated = plan.model_copy(update={"status": "running"})
        try:
            stored = await self.save_plan(
                updated,
                event_type="confirm",
                event_id=request_id,
                publish=publish,
            )
        except PlanStateConflict:
            canonical = await self.get_plan(plan_id, tenant_id=tenant_id, user_id=user_id)
            if canonical is None:
                raise
            return PlanActionResponse(
                plan_id=plan_id,
                status=canonical.status,
                current_step_id=canonical.current_step_id,
                next_action=canonical.next_action,
                state_version=canonical.state_version,
            )
        return PlanActionResponse(
            plan_id=plan_id,
            status=stored.status,
            current_step_id=stored.current_step_id,
            next_action=stored.next_action,
            state_version=stored.state_version,
            transitioned=True,
        )

    async def complete_host_managed_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        tenant_id: str,
        user_id: str,
        completion: HostManagedStepCompletion,
        publish: bool = True,
    ) -> PlanActionResponse:
        """Advance exactly the current Host-managed Step without creating a Run or Agent Event."""

        for _ in range(3):
            plan = await self.get_plan(plan_id, tenant_id=tenant_id, user_id=user_id)
            if plan is None:
                raise ValueError("Plan not found")
            if plan.last_event_id == completion.request_id:
                return _plan_action_response(plan, transitioned=True)
            if (
                plan.state_version != completion.expected_state_version
                or plan.status in {"completed", "failed", "cancelled"}
                or plan.current_step_id != step_id
            ):
                return _plan_action_response(plan)

            current_step = next((step for step in plan.steps if step.step_id == step_id), None)
            if (
                current_step is None
                or current_step.agent_id != completion.agent_id
                or plan.next_action is None
                or plan.next_action.type != "open_ui"
                or plan.next_action.step_id != step_id
                or plan.next_action.agent_id != completion.agent_id
            ):
                return _plan_action_response(plan)

            updated_steps = []
            for step in plan.steps:
                if step.step_id != step_id:
                    updated_steps.append(step)
                    continue
                updated_steps.append(
                    step.model_copy(
                        update={"status": _advance_step_status(step.status, "completed")}
                    )
                )
            current_step_id = _next_step_id(updated_steps)
            updated = plan.model_copy(
                update={
                    "steps": updated_steps,
                    "current_step_id": current_step_id,
                    "status": _plan_status(updated_steps, current_step_id),
                    "next_action": None,
                }
            )
            try:
                stored = await self.save_plan(
                    updated,
                    event_type="update",
                    event_id=completion.request_id,
                    publish=publish,
                )
            except PlanStateConflict:
                continue
            return _plan_action_response(stored, transitioned=True)

        canonical = await self.get_plan(plan_id, tenant_id=tenant_id, user_id=user_id)
        if canonical is None:
            raise ValueError("Plan not found")
        return _plan_action_response(canonical)

    async def cancel(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        publish: bool = True,
    ) -> PlanActionResponse:
        now = datetime.now(UTC)
        transition = await self.repository.cancel_unstarted(
            plan_id,
            tenant_id=tenant_id,
            user_id=user_id,
            run_repository=self.run_repository,
            now=now,
            formation_suppressed=(not publish or not self.automatic_formation_enabled),
        )
        if transition is None:
            raise ValueError("Plan not found")
        plan = transition.plan
        if transition.outcome == "already_cancelled":
            return PlanActionResponse(
                plan_id=plan_id,
                status="cancelled",
                current_step_id=None,
                state_version=plan.state_version,
            )
        if transition.outcome == "terminal_conflict":
            raise ValueError(f"Plan cannot be cancelled from status={plan.status}")
        if transition.outcome == "control_unsupported":
            return PlanActionResponse(
                plan_id=plan_id,
                status=plan.status,
                current_step_id=plan.current_step_id,
                next_action=plan.next_action,
                state_version=plan.state_version,
                accepted=False,
                transitioned=False,
                reason_code="control_unsupported",
            )
        if publish:
            await self._publish(
                plan,
                event_type="cancel",
                event_id=None,
                occurred_at=now,
            )
        return PlanActionResponse(
            plan_id=plan_id,
            status="cancelled",
            current_step_id=None,
            state_version=plan.state_version,
            transitioned=True,
        )

    async def apply_agent_event(
        self,
        event: AgentEvent,
        *,
        tenant_id: str,
        user_id: str,
        publish: bool = True,
    ) -> Plan | None:
        for _ in range(3):
            try:
                return await self._apply_agent_event_once(
                    event,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    publish=publish,
                )
            except PlanStateConflict:
                continue
        raise PlanStateConflict("Plan event update conflicted repeatedly")

    async def _apply_agent_event_once(
        self,
        event: AgentEvent,
        *,
        tenant_id: str,
        user_id: str,
        publish: bool,
    ) -> Plan | None:
        if not event.plan_id:
            return None
        plan = await self.get_plan(event.plan_id, tenant_id=tenant_id, user_id=user_id)
        if plan is None:
            return None
        if plan.last_event_id == event.event_id:
            if publish:
                await self._publish(
                    plan,
                    event_type="update",
                    event_id=event.event_id,
                    occurred_at=event.created_at,
                )
            return plan
        if plan.status in {"completed", "cancelled", "failed"}:
            return plan
        updated_steps = []
        event_status = _event_status_to_step_status(event.status, event.event_type)
        for step in plan.steps:
            if step.step_id != event.step_id:
                updated_steps.append(step)
                continue
            updated_steps.append(
                step.model_copy(update={"status": _advance_step_status(step.status, event_status)})
            )
        if updated_steps == plan.steps:
            return plan
        current_step_id = _next_step_id(updated_steps)
        plan_status = _plan_status(updated_steps, current_step_id)
        updated = plan.model_copy(
            update={
                "steps": updated_steps,
                "current_step_id": current_step_id,
                "status": plan_status,
                "last_event_id": event.event_id,
                "next_action": (
                    None if event_status in {"completed", "failed"} else plan.next_action
                ),
            }
        )
        return await self.save_plan(
            updated,
            event_type="update",
            event_id=event.event_id,
            occurred_at=event.created_at,
            publish=publish,
        )

    async def _publish(
        self,
        plan: Plan,
        *,
        event_type: str,
        event_id: str | None,
        occurred_at: datetime | None,
    ) -> None:
        if self.structured_formation is None:
            return
        try:
            await self.structured_formation.publish_plan(
                plan,
                event_type=event_type,
                event_id=event_id,
                source_version=event_id or f"plan-state-{plan.state_version}",
                source_order=plan.state_version,
                occurred_at=plan.updated_at,
            )
            await self._mark_formation_handled(plan)
        except Exception:
            return

    async def _mark_formation_handled(self, plan: Plan) -> None:
        marker = getattr(self.repository, "mark_formation_published", None)
        if marker is not None:
            await marker(plan.plan_id, state_version=plan.state_version)

    def new_plan_id(self) -> str:
        return f"plan_{uuid4().hex}"


def _event_status_to_step_status(status: str | None, event_type: str) -> str:
    expected = {
        "agent_started": "running",
        "agent_progress": "running",
        "agent_result": "completed",
        "agent_error": "failed",
        "agent_clarify": "blocked",
        "agent_cancelled": "cancelled",
    }.get(event_type)
    if expected is None:
        raise ValueError(f"Unsupported Agent event type: {event_type}")
    if status is not None and status != expected:
        raise ValueError(f"Agent event status={status} conflicts with event_type={event_type}")
    return expected


def validate_agent_event_contract(event: AgentEvent) -> None:
    _event_status_to_step_status(event.status, event.event_type)


def _advance_step_status(current: str, proposed: str) -> str:
    if current in {"completed", "failed", "cancelled"}:
        return current
    if current == "blocked" and proposed == "running":
        return current
    if proposed == "pending" and current != "pending":
        return current
    allowed = {
        "pending": {"pending", "running", "blocked", "completed", "failed", "cancelled"},
        "running": {"running", "blocked", "completed", "failed", "cancelled"},
        "blocked": {"running", "blocked", "completed", "failed", "cancelled"},
    }
    if proposed not in allowed.get(current, set()):
        raise ValueError(f"Illegal Plan step transition: {current} -> {proposed}")
    return proposed


def _next_step_id(steps) -> str | None:
    completed = {step.step_id for step in steps if step.status == "completed"}
    for step in steps:
        if step.status == "pending" and all(parent in completed for parent in step.depends_on):
            return step.step_id
        if step.status in {"running", "blocked", "failed"}:
            return step.step_id
    return None


def _plan_status(steps, current_step_id: str | None) -> str:
    if any(step.status == "cancelled" for step in steps):
        return "cancelled"
    if any(step.status == "failed" for step in steps):
        return "failed"
    if any(step.status == "blocked" for step in steps):
        return "blocked"
    if all(step.status == "completed" for step in steps):
        return "completed"
    if current_step_id:
        return "running"
    return "pending"


def _plan_action_response(plan: Plan, *, transitioned: bool = False) -> PlanActionResponse:
    return PlanActionResponse(
        plan_id=plan.plan_id,
        status=plan.status,
        current_step_id=plan.current_step_id,
        next_action=plan.next_action,
        state_version=plan.state_version,
        transitioned=transitioned,
    )
