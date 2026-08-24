import asyncio
import hashlib
from datetime import UTC, datetime

from app.core.errors import RegistryVersionConflict
from app.core.redaction import redact_value
from app.repositories.interfaces import PlanCancelTransition
from app.schemas.agents import AgentDefinitionV2
from app.schemas.events import AgentEvent, ConversationEvent
from app.schemas.logs import AgentResult, AgentRun, RouteLog
from app.schemas.plans import Plan
from app.schemas.registry_audit import RegistryAuditRecord
from app.schemas.registry_mutation import RegistryMutationCommand, RegistryMutationResult
from app.schemas.sessions import ChatMessage


class MemoryAgentDefinitionRepository:
    def __init__(self, agents: list[AgentDefinitionV2] | None = None) -> None:
        self.agents = {agent.agent_id: agent for agent in agents or []}
        self.registry_audit: list[RegistryAuditRecord] = []
        self._mutation_lock = asyncio.Lock()

    async def list(self, *, enabled_only: bool = False) -> list[AgentDefinitionV2]:
        values = list(self.agents.values())
        if enabled_only:
            values = [agent for agent in values if agent.enabled]
        return sorted(values, key=lambda item: (-item.priority, item.agent_id))

    async def get(self, agent_id: str) -> AgentDefinitionV2 | None:
        return self.agents.get(agent_id)

    async def upsert(
        self, definition: AgentDefinitionV2, *, expected_revision: int | None = None
    ) -> AgentDefinitionV2:
        current = self.agents.get(definition.agent_id)
        revision = current.revision if current else 0
        if expected_revision is not None and revision != expected_revision:
            raise RegistryVersionConflict("Agent revision conflict")
        saved = definition.model_copy(update={"revision": revision + 1})
        self.agents[definition.agent_id] = saved
        return saved

    async def set_enabled(
        self, agent_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> AgentDefinitionV2 | None:
        agent = self.agents.get(agent_id)
        if agent is None:
            return None
        if expected_revision is not None and agent.revision != expected_revision:
            raise RegistryVersionConflict("Agent revision conflict")
        updated = agent.model_copy(update={"enabled": enabled, "revision": agent.revision + 1})
        self.agents[agent_id] = updated
        return updated

    async def delete(self, agent_id: str, *, expected_revision: int | None = None) -> bool:
        current = self.agents.get(agent_id)
        if current is not None and expected_revision is not None:
            if current.revision != expected_revision:
                raise RegistryVersionConflict("Agent revision conflict")
        return self.agents.pop(agent_id, None) is not None

    async def mutate(self, command: RegistryMutationCommand) -> RegistryMutationResult:
        async with self._mutation_lock:
            before = self.agents.get(command.agent_id)
            current_revision = before.revision if before is not None else 0
            if current_revision != command.expected_revision:
                raise RegistryVersionConflict("Agent revision conflict")
            if command.operation in {"create", "update"}:
                if command.operation == "create" and before is not None:
                    raise RegistryVersionConflict("Agent revision conflict")
                if command.operation == "update" and before is None:
                    raise RegistryVersionConflict("Agent revision conflict")
                after = command.definition.model_copy(update={"revision": current_revision + 1})
                self.agents[command.agent_id] = after
            elif command.operation in {"enable", "disable"}:
                if before is None:
                    raise RegistryVersionConflict("Agent revision conflict")
                after = before.model_copy(
                    update={
                        "enabled": command.operation == "enable",
                        "revision": current_revision + 1,
                    }
                )
                self.agents[command.agent_id] = after
            else:
                if before is None:
                    raise RegistryVersionConflict("Agent revision conflict")
                after = None
                del self.agents[command.agent_id]
            revision = after.revision if after is not None else current_revision + 1
            audit = RegistryAuditRecord(
                revision_id=command.revision_id,
                agent_id=command.agent_id,
                revision=revision,
                operation=command.operation,
                operator_id=command.actor_id,
                source=command.source,
                before=redact_value(before.model_dump(mode="json")) if before else None,
                after=redact_value(after.model_dump(mode="json")) if after else None,
                created_at=command.created_at,
            )
            self.registry_audit.append(audit)
            return RegistryMutationResult(
                operation=command.operation,
                before=before,
                after=after,
                audit=audit,
            )


class MemoryMessageRepository:
    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []

    async def add(self, message: ChatMessage) -> ChatMessage:
        self.messages.append(message)
        return message

    async def list_by_session(
        self,
        session_id: str,
        *,
        tenant_id: str,
        user_id: str,
        source: str | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[ChatMessage]:
        items = [
            item
            for item in self.messages
            if item.session_id == session_id
            and item.tenant_id == tenant_id
            and item.user_id == user_id
        ]
        if source:
            items = [item for item in items if item.source == source]
        if agent_id:
            items = [item for item in items if item.agent_id == agent_id]
        return items[-limit:]


class MemoryEventRepository:
    def __init__(self) -> None:
        self.conversation_events: list[ConversationEvent] = []
        self.agent_events: dict[str, AgentEvent] = {}

    async def add_conversation_event(self, event: ConversationEvent) -> ConversationEvent:
        self.conversation_events.append(event)
        return event

    async def add_agent_event(self, event: AgentEvent) -> tuple[AgentEvent, bool]:
        existing = self.agent_events.get(event.event_id)
        if existing:
            return existing.model_copy(deep=True), True
        stored = event.model_copy(update={"created_at": datetime.now(UTC)}, deep=True)
        self.agent_events[event.event_id] = stored
        return stored.model_copy(deep=True), False

    async def get_event(
        self, event_id: str, *, tenant_id: str | None, user_id: str | None
    ) -> AgentEvent | None:
        event = self.agent_events.get(event_id)
        if event is None or event.tenant_id != tenant_id or event.user_id != user_id:
            return None
        return event.model_copy(deep=True)

    async def list_recent_events(
        self, session_id: str, *, tenant_id: str, user_id: str, limit: int = 10
    ) -> list[AgentEvent]:
        bounded_limit = max(0, limit)
        items = [
            item
            for item in self.agent_events.values()
            if item.session_id == session_id
            and item.tenant_id == tenant_id
            and item.user_id == user_id
        ]
        return list(reversed(items[-bounded_limit:])) if bounded_limit else []


class MemoryRunRepository:
    def __init__(self) -> None:
        self.runs: dict[str, AgentRun] = {}
        self.formation_published_order: dict[str, int] = {}
        # Completion stores are short-lived application collaborators.  Keep
        # the serialization primitive with the shared persistence state so
        # two service instances using these repositories cannot both turn the
        # same accepted Run into a terminal record.
        self._invocation_completion_lock = asyncio.Lock()

    async def add_run(self, run: AgentRun) -> AgentRun:
        stored = AgentRun.model_validate(run.model_dump(mode="python"))
        self.runs[run.run_id] = stored
        return stored.model_copy(deep=True)

    async def update_run(self, run: AgentRun) -> AgentRun:
        existing = self.runs.get(run.run_id)
        if existing is None:
            raise ValueError("Agent run not found")
        _validate_run_identity(existing, run)
        stored = AgentRun.model_validate(run.model_dump(mode="python"))
        self.runs[run.run_id] = stored
        return stored.model_copy(deep=True)

    async def get_run(self, run_id: str) -> AgentRun | None:
        run = self.runs.get(run_id)
        return run.model_copy(deep=True) if run is not None else None

    async def get_owned_run(self, run_id: str, *, tenant_id: str, user_id: str) -> AgentRun | None:
        run = self.runs.get(run_id)
        if run is None or run.tenant_id != tenant_id or run.user_id != user_id:
            return None
        return run.model_copy(deep=True)

    async def get_active_delegated_run_for_plan(
        self, plan_id: str, *, tenant_id: str, user_id: str
    ) -> AgentRun | None:
        run = next(
            (
                item
                for item in self.runs.values()
                if item.plan_id == plan_id
                and item.tenant_id == tenant_id
                and item.user_id == user_id
                and item.delegated
                and item.status in {"pending", "running", "blocked"}
            ),
            None,
        )
        return run.model_copy(deep=True) if run is not None else None

    async def list_formation_pending(self, *, limit: int = 100) -> list[AgentRun]:
        pending = [
            run
            for run in self.runs.values()
            if not run.formation_suppressed
            and self.formation_published_order.get(run.run_id, 0) < _run_source_order(run)
        ]
        return [item.model_copy(deep=True) for item in pending[:limit]]

    async def mark_formation_published(self, run_id: str, *, source_order: int) -> None:
        if run_id in self.runs:
            self.formation_published_order[run_id] = max(
                source_order,
                self.formation_published_order.get(run_id, 0),
            )

    async def get_formation_published_order(self, run_id: str) -> int:
        return self.formation_published_order.get(run_id, 0)


class MemoryResultRepository:
    def __init__(self) -> None:
        self.results: list[AgentResult] = []
        self.formation_published: set[str] = set()
        self.turn_captured: set[str] = set()

    async def add_result(self, result: AgentResult) -> AgentResult:
        stored = result.model_copy(deep=True)
        self.results.append(stored)
        if stored.formation_suppressed and not stored.formation_skip_audit_required:
            self.turn_captured.add(stored.result_id)
        return stored.model_copy(deep=True)

    async def list_recent(
        self, session_id: str, *, tenant_id: str, user_id: str, limit: int = 5
    ) -> list[AgentResult]:
        items = [
            item
            for item in self.results
            if item.session_id == session_id
            and item.tenant_id == tenant_id
            and item.user_id == user_id
        ]
        return list(reversed(items[-limit:]))

    async def list_formation_pending(self, *, limit: int = 100) -> list[AgentResult]:
        pending = [
            item
            for item in self.results
            if (
                (not item.formation_suppressed and item.result_id not in self.formation_published)
                or item.result_id not in self.turn_captured
            )
        ]
        return [item.model_copy(deep=True) for item in pending[:limit]]

    async def mark_formation_published(self, result_id: str) -> None:
        self.formation_published.add(result_id)

    async def mark_turn_captured(self, result_id: str) -> None:
        self.turn_captured.add(result_id)


def _validate_run_identity(existing: AgentRun, incoming: AgentRun) -> None:
    identity_fields = (
        "request_id",
        "session_id",
        "agent_id",
        "user_id",
        "tenant_id",
        "plan_id",
        "step_id",
        "agent_revision",
        "handling_kind",
        "binding_snapshot",
    )
    if any(getattr(existing, field) != getattr(incoming, field) for field in identity_fields):
        raise ValueError("Agent run identity cannot be changed")


class MemoryPlanRepository:
    def __init__(self) -> None:
        self.plans: dict[str, Plan] = {}
        self.execution_claims: dict[str, tuple[str, str, datetime, int, str, int]] = {}
        self.execution_attempts: dict[str, int] = {}
        self.formation_published_version: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._delegated_run_lock = asyncio.Lock()

    @property
    def delegated_run_lock(self) -> asyncio.Lock:
        return self._delegated_run_lock

    async def save(self, plan: Plan, *, formation_suppressed: bool = False) -> Plan:
        async with self._lock:
            existing = self.plans.get(plan.plan_id)
            if existing and (
                existing.tenant_id != plan.tenant_id or existing.user_id != plan.user_id
            ):
                raise ValueError("Plan ownership cannot be changed")
            stored = plan.model_copy(deep=True)
            self.plans[plan.plan_id] = stored
            claim = self.execution_claims.get(plan.plan_id)
            if claim is not None and plan.status == "running":
                self.execution_claims[plan.plan_id] = (
                    *claim[:3],
                    plan.state_version,
                    claim[4],
                    claim[5],
                )
            else:
                self.execution_claims.pop(plan.plan_id, None)
            if formation_suppressed:
                self.formation_published_version[plan.plan_id] = plan.state_version
            return stored.model_copy(deep=True)

    async def get(self, plan_id: str, *, tenant_id: str, user_id: str) -> Plan | None:
        plan = self.plans.get(plan_id)
        if plan is None or plan.tenant_id != tenant_id or plan.user_id != user_id:
            return None
        return plan.model_copy(deep=True)

    async def cancel_unstarted(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        run_repository,
        now: datetime,
        formation_suppressed: bool = False,
    ) -> PlanCancelTransition | None:
        async with self._delegated_run_lock, self._lock:
            plan = self.plans.get(plan_id)
            if plan is None or plan.tenant_id != tenant_id or plan.user_id != user_id:
                return None
            if plan.status == "cancelled":
                return PlanCancelTransition(plan.model_copy(deep=True), "already_cancelled")
            if plan.status in {"completed", "failed"}:
                return PlanCancelTransition(plan.model_copy(deep=True), "terminal_conflict")
            active_run = (
                await run_repository.get_active_delegated_run_for_plan(
                    plan_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                if run_repository is not None
                else None
            )
            if active_run is not None or any(step.status == "running" for step in plan.steps):
                return PlanCancelTransition(plan.model_copy(deep=True), "control_unsupported")
            updated = plan.model_copy(
                update={
                    "status": "cancelled",
                    "steps": [
                        step.model_copy(update={"status": "cancelled"})
                        if step.status in {"pending", "blocked"}
                        else step
                        for step in plan.steps
                    ],
                    "current_step_id": None,
                    "next_action": None,
                    "state_version": plan.state_version + 1,
                    "updated_at": now,
                    "formation_event_type": "cancel",
                }
            )
            self.plans[plan_id] = updated.model_copy(deep=True)
            self.execution_claims.pop(plan_id, None)
            if formation_suppressed:
                self.formation_published_version[plan_id] = updated.state_version
            return PlanCancelTransition(updated.model_copy(deep=True), "cancelled")

    async def save_if_version(
        self,
        plan: Plan,
        *,
        expected_version: int,
        formation_suppressed: bool = False,
    ) -> Plan | None:
        async with self._lock:
            existing = self.plans.get(plan.plan_id)
            if (
                existing is None
                or existing.tenant_id != plan.tenant_id
                or existing.user_id != plan.user_id
                or existing.state_version != expected_version
            ):
                return None
            stored = plan.model_copy(deep=True)
            self.plans[plan.plan_id] = stored
            claim = self.execution_claims.get(plan.plan_id)
            if claim is not None and plan.status == "running":
                self.execution_claims[plan.plan_id] = (
                    *claim[:3],
                    plan.state_version,
                    claim[4],
                    claim[5],
                )
            else:
                self.execution_claims.pop(plan.plan_id, None)
            if formation_suppressed:
                self.formation_published_version[plan.plan_id] = plan.state_version
            return stored.model_copy(deep=True)

    async def get_active_by_session(
        self, session_id: str, *, tenant_id: str, user_id: str
    ) -> Plan | None:
        active = [
            plan
            for plan in self.plans.values()
            if plan.session_id == session_id
            and plan.tenant_id == tenant_id
            and plan.user_id == user_id
            and plan.status in {"pending", "running", "blocked"}
        ]
        return active[-1].model_copy(deep=True) if active else None

    async def claim_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str,
        lease_expires_at: datetime,
        now: datetime,
        formation_suppressed: bool = False,
    ) -> Plan | None:
        async with self._lock:
            plan = self.plans.get(plan_id)
            if (
                plan is None
                or plan.tenant_id != tenant_id
                or plan.user_id != user_id
                or plan.status not in {"pending", "running", "blocked"}
            ):
                return None
            existing_claim = self.execution_claims.get(plan_id)
            expired = bool(existing_claim and existing_claim[2] <= now)
            if existing_claim and not expired:
                return None
            step = next((item for item in plan.steps if item.step_id == step_id), None)
            allowed_statuses = {"pending", "blocked"}
            if expired and existing_claim and existing_claim[0] == step_id:
                allowed_statuses.add("running")
            if step is None or step.status not in allowed_statuses:
                return None
            recovering = bool(
                expired and existing_claim and existing_claim[0] == step_id and existing_claim[4]
            )
            attempt = (
                existing_claim[5]
                if recovering and existing_claim is not None
                else self.execution_attempts.get(plan_id, 0) + 1
            )
            execution_key = (
                existing_claim[4]
                if recovering and existing_claim is not None
                else _execution_key(plan_id, step_id, attempt)
            )
            updated_steps = [
                item.model_copy(update={"status": "running"}) if item.step_id == step_id else item
                for item in plan.steps
            ]
            claimed = plan.model_copy(
                update={
                    "steps": updated_steps,
                    "current_step_id": step_id,
                    "status": "running",
                    "next_action": None,
                    "state_version": plan.state_version + 1,
                    "updated_at": now,
                    "formation_event_type": "update",
                },
                deep=True,
            )
            self.plans[plan_id] = claimed
            self.execution_claims[plan_id] = (
                step_id,
                claim_id,
                lease_expires_at,
                claimed.state_version,
                execution_key,
                attempt,
            )
            self.execution_attempts[plan_id] = attempt
            if formation_suppressed:
                self.formation_published_version[plan_id] = claimed.state_version
            return claimed.model_copy(deep=True)

    async def save_claimed_step(
        self,
        plan: Plan,
        step_id: str,
        *,
        claim_id: str,
        formation_suppressed: bool = False,
    ) -> Plan | None:
        async with self._lock:
            claim = self.execution_claims.get(plan.plan_id)
            if claim is None or claim[0] != step_id or claim[1] != claim_id:
                return None
            existing = self.plans.get(plan.plan_id)
            if existing is None or (
                existing.tenant_id != plan.tenant_id or existing.user_id != plan.user_id
            ):
                return None
            if existing.state_version != claim[3] or plan.state_version != claim[3] + 1:
                return None
            stored = plan.model_copy(update={"updated_at": datetime.now(UTC)}, deep=True)
            self.plans[plan.plan_id] = stored
            self.execution_claims.pop(plan.plan_id, None)
            if formation_suppressed:
                self.formation_published_version[plan.plan_id] = stored.state_version
            return stored.model_copy(deep=True)

    async def list_formation_pending(self, *, limit: int = 100) -> list[Plan]:
        values = [
            plan
            for plan in self.plans.values()
            if self.formation_published_version.get(plan.plan_id, 0) < plan.state_version
        ]
        return [item.model_copy(deep=True) for item in values[:limit]]

    async def mark_formation_published(self, plan_id: str, *, state_version: int) -> None:
        if plan_id in self.plans:
            self.formation_published_version[plan_id] = max(
                state_version,
                self.formation_published_version.get(plan_id, 0),
            )

    async def get_execution_claim_key(self, plan_id: str, *, claim_id: str) -> str | None:
        claim = self.execution_claims.get(plan_id)
        return claim[4] if claim is not None and claim[1] == claim_id else None

    async def renew_step_claim(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        user_id: str,
        claim_id: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> bool:
        async with self._lock:
            plan = self.plans.get(plan_id)
            claim = self.execution_claims.get(plan_id)
            if (
                plan is None
                or plan.tenant_id != tenant_id
                or plan.user_id != user_id
                or claim is None
                or claim[1] != claim_id
                or claim[2] <= now
            ):
                return False
            self.execution_claims[plan_id] = (
                claim[0],
                claim[1],
                lease_expires_at,
                claim[3],
                claim[4],
                claim[5],
            )
            return True


class MemoryRouteLogRepository:
    def __init__(self) -> None:
        self.logs: list[RouteLog] = []

    async def add(self, log: RouteLog) -> RouteLog:
        stored = RouteLog.model_validate(log.model_dump(mode="python"))
        self.logs.append(stored)
        return stored


def _run_source_order(run: AgentRun) -> int:
    if run.status == "running":
        return 1
    if run.status in {"blocked", "clarify"}:
        return 2
    return 3


def _execution_key(plan_id: str, step_id: str, attempt: int) -> str:
    identity = "\x1f".join((plan_id, step_id, str(attempt)))
    return f"plan_exec_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
