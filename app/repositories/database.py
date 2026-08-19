import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, case, delete, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import RegistryVersionConflict
from app.core.redaction import redact_value
from app.db.models import (
    AgentDefinitionModel,
    AgentEventModel,
    AgentResultModel,
    AgentRunModel,
    ChatMessageModel,
    ConversationEventModel,
    PlanModel,
    PlanStepModel,
    RegistryRevisionModel,
    RouteLogModel,
)
from app.repositories.interfaces import PlanCancelTransition
from app.repositories.json_utils import dumps, loads
from app.schemas.agents import AgentDefinition
from app.schemas.events import AgentEvent, ConversationEvent
from app.schemas.logs import AgentResult, AgentRun, RouteLog
from app.schemas.plans import Plan, PlanStep
from app.schemas.registry_audit import RegistryAuditRecord
from app.schemas.registry_mutation import RegistryMutationCommand, RegistryMutationResult
from app.schemas.sessions import ChatMessage


class DatabaseAgentDefinitionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def list(self, *, enabled_only: bool = False) -> list[AgentDefinition]:
        async with self.session_factory() as session:
            stmt = select(AgentDefinitionModel).order_by(
                desc(AgentDefinitionModel.priority),
                AgentDefinitionModel.agent_id,
            )
            if enabled_only:
                stmt = stmt.where(AgentDefinitionModel.enabled.is_(True))
            rows = (await session.execute(stmt)).scalars().all()
            return [_agent_from_row(row) for row in rows]

    async def get(self, agent_id: str) -> AgentDefinition | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == agent_id)
            )
            return _agent_from_row(row) if row else None

    async def upsert(
        self, definition: AgentDefinition, *, expected_revision: int | None = None
    ) -> AgentDefinition:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentDefinitionModel).where(
                    AgentDefinitionModel.agent_id == definition.agent_id
                )
            )
            values = _agent_values(definition, source="database")
            if row is None:
                if expected_revision not in {None, 0}:
                    raise RegistryVersionConflict("Agent revision conflict")
                values["revision"] = 1
                row = AgentDefinitionModel(**values)
                session.add(row)
            else:
                if expected_revision is not None and row.revision != expected_revision:
                    raise RegistryVersionConflict("Agent revision conflict")
                values["revision"] = row.revision + 1
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _agent_from_row(row)

    async def set_enabled(
        self, agent_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> AgentDefinition | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == agent_id)
            )
            if row is None:
                return None
            if expected_revision is not None and row.revision != expected_revision:
                raise RegistryVersionConflict("Agent revision conflict")
            row.enabled = enabled
            row.revision += 1
            await session.commit()
            await session.refresh(row)
            return _agent_from_row(row)

    async def delete(self, agent_id: str, *, expected_revision: int | None = None) -> bool:
        async with self.session_factory() as session:
            if expected_revision is not None:
                current = await session.scalar(
                    select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == agent_id)
                )
                if current is None:
                    return False
                if current.revision != expected_revision:
                    raise RegistryVersionConflict("Agent revision conflict")
            result = await session.execute(
                delete(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == agent_id)
            )
            await session.commit()
            return bool(result.rowcount)

    async def mutate(self, command: RegistryMutationCommand) -> RegistryMutationResult:
        async with self.session_factory() as session, session.begin():
            row = await session.scalar(
                select(AgentDefinitionModel)
                .where(AgentDefinitionModel.agent_id == command.agent_id)
                .with_for_update()
            )
            before = _agent_from_row(row) if row is not None else None
            current_revision = before.revision if before is not None else 0
            if current_revision != command.expected_revision:
                raise RegistryVersionConflict("Agent revision conflict")

            if command.operation == "create":
                if row is not None:
                    raise RegistryVersionConflict("Agent revision conflict")
                after = command.definition.model_copy(update={"revision": 1})
                row = AgentDefinitionModel(**_agent_values(after, source="database"))
                session.add(row)
            elif command.operation == "update":
                if row is None:
                    raise RegistryVersionConflict("Agent revision conflict")
                after = command.definition.model_copy(update={"revision": current_revision + 1})
                for key, value in _agent_values(after, source="database").items():
                    setattr(row, key, value)
            elif command.operation in {"enable", "disable"}:
                if row is None:
                    raise RegistryVersionConflict("Agent revision conflict")
                row.enabled = command.operation == "enable"
                row.revision = current_revision + 1
                after = before.model_copy(
                    update={
                        "enabled": command.operation == "enable",
                        "revision": current_revision + 1,
                    }
                )
            else:
                if row is None:
                    raise RegistryVersionConflict("Agent revision conflict")
                after = None
                await session.delete(row)

            revision = after.revision if after is not None else current_revision + 1
            audit = _mutation_audit(command, before=before, after=after, revision=revision)
            session.add(
                RegistryRevisionModel(
                    revision_id=audit.revision_id,
                    agent_id=audit.agent_id,
                    revision=audit.revision,
                    operation=audit.operation,
                    operator_id=audit.operator_id,
                    source=audit.source,
                    before_text=dumps(audit.before) if audit.before is not None else None,
                    after_text=dumps(audit.after) if audit.after is not None else None,
                    created_at=audit.created_at,
                )
            )
            await session.flush()
            return RegistryMutationResult(
                operation=command.operation,
                before=before,
                after=after,
                audit=audit,
            )


class DatabaseMessageRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add(self, message: ChatMessage) -> ChatMessage:
        async with self.session_factory() as session:
            payload = message.model_dump()
            payload["metadata_text"] = dumps(payload.pop("metadata"))
            payload.pop("created_at", None)
            row = ChatMessageModel(**payload)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _message_from_row(row)

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
        async with self.session_factory() as session:
            stmt = select(ChatMessageModel).where(
                ChatMessageModel.session_id == session_id,
                ChatMessageModel.tenant_id == tenant_id,
                ChatMessageModel.user_id == user_id,
            )
            if source:
                stmt = stmt.where(ChatMessageModel.source == source)
            if agent_id:
                stmt = stmt.where(ChatMessageModel.agent_id == agent_id)
            stmt = stmt.order_by(desc(ChatMessageModel.created_at)).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return list(reversed([_message_from_row(row) for row in rows]))


class DatabaseEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add_conversation_event(self, event: ConversationEvent) -> ConversationEvent:
        async with self.session_factory() as session:
            payload = event.model_dump()
            payload["payload_text"] = dumps(payload.pop("payload"))
            payload.pop("created_at", None)
            row = ConversationEventModel(**payload)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _conversation_event_from_row(row)

    async def add_agent_event(self, event: AgentEvent) -> tuple[AgentEvent, bool]:
        async with self.session_factory() as session:
            existing = await session.get(AgentEventModel, event.event_id)
            if existing:
                return _agent_event_from_row(existing), True
            payload = event.model_dump()
            payload["payload_text"] = dumps(payload.pop("payload"))
            payload.pop("created_at", None)
            row = AgentEventModel(**payload)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _agent_event_from_row(row), False

    async def get_event(
        self, event_id: str, *, tenant_id: str | None, user_id: str | None
    ) -> AgentEvent | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentEventModel).where(
                    AgentEventModel.event_id == event_id,
                    AgentEventModel.tenant_id == tenant_id,
                    AgentEventModel.user_id == user_id,
                )
            )
            return _agent_event_from_row(row) if row else None

    async def list_recent_events(
        self, session_id: str, *, tenant_id: str, user_id: str, limit: int = 10
    ) -> list[AgentEvent]:
        async with self.session_factory() as session:
            stmt = (
                select(AgentEventModel)
                .where(
                    AgentEventModel.session_id == session_id,
                    AgentEventModel.tenant_id == tenant_id,
                    AgentEventModel.user_id == user_id,
                )
                .order_by(desc(AgentEventModel.created_at))
                .limit(max(0, limit))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_agent_event_from_row(row) for row in rows]


class DatabaseRunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add_run(self, run: AgentRun) -> AgentRun:
        async with self.session_factory() as session:
            row = AgentRunModel(**_run_values(run))
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _run_from_row(row)

    async def update_run(self, run: AgentRun) -> AgentRun:
        async with self.session_factory() as session:
            row = await session.get(AgentRunModel, run.run_id)
            if row is None:
                raise ValueError("Agent run not found")
            _validate_run_identity(_run_from_row(row), run)
            values = _run_values(run)
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _run_from_row(row)

    async def get_run(self, run_id: str) -> AgentRun | None:
        async with self.session_factory() as session:
            row = await session.get(AgentRunModel, run_id)
            return _run_from_row(row) if row else None

    async def get_owned_run(self, run_id: str, *, tenant_id: str, user_id: str) -> AgentRun | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentRunModel).where(
                    AgentRunModel.run_id == run_id,
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.user_id == user_id,
                )
            )
            return _run_from_row(row) if row else None

    async def get_active_delegated_run_for_plan(
        self, plan_id: str, *, tenant_id: str, user_id: str
    ) -> AgentRun | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.plan_id == plan_id,
                    AgentRunModel.tenant_id == tenant_id,
                    AgentRunModel.user_id == user_id,
                    AgentRunModel.delegated.is_(True),
                    AgentRunModel.status.in_(["pending", "running", "blocked"]),
                )
                .order_by(AgentRunModel.created_at, AgentRunModel.run_id)
                .limit(1)
            )
            return _run_from_row(row) if row else None

    async def list_formation_pending(self, *, limit: int = 100) -> list[AgentRun]:
        async with self.session_factory() as session:
            expected_order = case(
                (AgentRunModel.status == "running", 1),
                (AgentRunModel.status.in_(["blocked", "clarify"]), 2),
                else_=3,
            )
            rows = (
                (
                    await session.execute(
                        select(AgentRunModel)
                        .where(
                            AgentRunModel.formation_suppressed.is_(False),
                            AgentRunModel.formation_published_order < expected_order,
                        )
                        .order_by(AgentRunModel.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_run_from_row(row) for row in rows]

    async def mark_formation_published(self, run_id: str, *, source_order: int) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(AgentRunModel)
                .where(
                    AgentRunModel.run_id == run_id,
                    AgentRunModel.formation_published_order < source_order,
                )
                .values(formation_published_order=source_order)
            )
            await session.commit()

    async def get_formation_published_order(self, run_id: str) -> int:
        async with self.session_factory() as session:
            value = await session.scalar(
                select(AgentRunModel.formation_published_order).where(
                    AgentRunModel.run_id == run_id
                )
            )
            return int(value or 0)


class DatabaseResultRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add_result(self, result: AgentResult) -> AgentResult:
        async with self.session_factory() as session:
            row = AgentResultModel(**_result_values(result))
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _result_from_row(row)

    async def list_recent(
        self, session_id: str, *, tenant_id: str, user_id: str, limit: int = 5
    ) -> list[AgentResult]:
        async with self.session_factory() as session:
            stmt = (
                select(AgentResultModel)
                .where(
                    AgentResultModel.session_id == session_id,
                    AgentResultModel.tenant_id == tenant_id,
                    AgentResultModel.user_id == user_id,
                )
                .order_by(desc(AgentResultModel.created_at))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_result_from_row(row) for row in rows]

    async def list_formation_pending(self, *, limit: int = 100) -> list[AgentResult]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentResultModel)
                        .where(
                            or_(
                                and_(
                                    AgentResultModel.formation_suppressed.is_(False),
                                    AgentResultModel.formation_published.is_(False),
                                ),
                                AgentResultModel.turn_captured.is_(False),
                            ),
                        )
                        .order_by(AgentResultModel.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_result_from_row(row) for row in rows]

    async def mark_formation_published(self, result_id: str) -> None:
        await self._mark(result_id, formation_published=True)

    async def mark_turn_captured(self, result_id: str) -> None:
        await self._mark(result_id, turn_captured=True)

    async def _mark(self, result_id: str, **values) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(AgentResultModel)
                .where(AgentResultModel.result_id == result_id)
                .values(**values)
            )
            await session.commit()


class DatabasePlanRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def save(self, plan: Plan, *, formation_suppressed: bool = False) -> Plan:
        async with self.session_factory() as session:
            row = await session.get(PlanModel, plan.plan_id)
            if row is not None and (row.tenant_id != plan.tenant_id or row.user_id != plan.user_id):
                raise ValueError("Plan ownership cannot be changed")
            metadata = {
                "execution_policy": plan.execution_policy,
                "next_action": plan.next_action.model_dump(mode="json")
                if plan.next_action
                else None,
                "last_event_id": plan.last_event_id,
                "state_version": plan.state_version,
                "formation_event_type": plan.formation_event_type,
            }
            values = {
                "plan_id": plan.plan_id,
                "session_id": plan.session_id or "",
                "user_id": plan.user_id,
                "tenant_id": plan.tenant_id,
                "status": plan.status,
                "current_step_id": plan.current_step_id,
                "state_version": plan.state_version,
                "formation_published_version": plan.state_version if formation_suppressed else 0,
                "original_query": dumps(metadata),
                "updated_at": plan.updated_at,
            }
            if row is None:
                row = PlanModel(**values)
                session.add(row)
            else:
                values.pop("formation_published_version")
                for key, value in values.items():
                    setattr(row, key, value)
                if formation_suppressed:
                    row.formation_published_version = plan.state_version
            if plan.status in {"completed", "failed", "cancelled"}:
                row.execution_claim_id = None
                row.execution_claim_step_id = None
                row.execution_claim_state_version = None
                row.execution_claim_key = None
                row.execution_claim_expires_at = None
            await session.execute(
                delete(PlanStepModel).where(PlanStepModel.plan_id == plan.plan_id)
            )
            for step in plan.steps:
                session.add(
                    PlanStepModel(
                        step_id=step.step_id,
                        plan_id=plan.plan_id,
                        agent_id=step.agent_id,
                        status=step.status,
                        description=step.description,
                        depends_on_text=dumps(step.depends_on),
                        artifact_refs_text=dumps([ref.model_dump() for ref in step.artifact_refs]),
                    )
                )
            await session.commit()
            return plan

    async def save_if_version(
        self,
        plan: Plan,
        *,
        expected_version: int,
        formation_suppressed: bool = False,
    ) -> Plan | None:
        async with self.session_factory() as session:
            metadata = {
                "execution_policy": plan.execution_policy,
                "next_action": plan.next_action.model_dump(mode="json")
                if plan.next_action
                else None,
                "last_event_id": plan.last_event_id,
                "state_version": plan.state_version,
                "formation_event_type": plan.formation_event_type,
            }
            saved = await session.execute(
                update(PlanModel)
                .where(
                    PlanModel.plan_id == plan.plan_id,
                    PlanModel.tenant_id == plan.tenant_id,
                    PlanModel.user_id == plan.user_id,
                    PlanModel.state_version == expected_version,
                )
                .values(
                    session_id=plan.session_id or "",
                    status=plan.status,
                    current_step_id=plan.current_step_id,
                    state_version=plan.state_version,
                    formation_published_version=(
                        plan.state_version
                        if formation_suppressed
                        else PlanModel.formation_published_version
                    ),
                    original_query=dumps(metadata),
                    updated_at=plan.updated_at,
                    execution_claim_id=(
                        PlanModel.execution_claim_id if plan.status == "running" else None
                    ),
                    execution_claim_step_id=(
                        PlanModel.execution_claim_step_id if plan.status == "running" else None
                    ),
                    execution_claim_state_version=case(
                        (PlanModel.execution_claim_id.is_not(None), plan.state_version),
                        else_=None,
                    )
                    if plan.status == "running"
                    else None,
                    execution_claim_key=(
                        PlanModel.execution_claim_key if plan.status == "running" else None
                    ),
                    execution_claim_expires_at=(
                        PlanModel.execution_claim_expires_at if plan.status == "running" else None
                    ),
                )
            )
            if saved.rowcount != 1:
                await session.rollback()
                return None
            await session.execute(
                delete(PlanStepModel).where(PlanStepModel.plan_id == plan.plan_id)
            )
            for step in plan.steps:
                session.add(
                    PlanStepModel(
                        step_id=step.step_id,
                        plan_id=plan.plan_id,
                        agent_id=step.agent_id,
                        status=step.status,
                        description=step.description,
                        depends_on_text=dumps(step.depends_on),
                        artifact_refs_text=dumps([ref.model_dump() for ref in step.artifact_refs]),
                    )
                )
            await session.commit()
            return plan

    async def get(self, plan_id: str, *, tenant_id: str, user_id: str) -> Plan | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(PlanModel).where(
                    PlanModel.plan_id == plan_id,
                    PlanModel.tenant_id == tenant_id,
                    PlanModel.user_id == user_id,
                )
            )
            if row is None:
                return None
            steps = (
                (
                    await session.execute(
                        select(PlanStepModel)
                        .where(PlanStepModel.plan_id == plan_id)
                        .order_by(PlanStepModel.id)
                    )
                )
                .scalars()
                .all()
            )
            return _plan_from_rows(row, steps)

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
        del run_repository
        async with self.session_factory() as session, session.begin():
            active_run = await _lock_active_delegated_run_for_plan(
                session,
                plan_id=plan_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            row = await _lock_plan_row(
                session,
                plan_id=plan_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if row is None:
                return None
            steps = (
                (
                    await session.execute(
                        select(PlanStepModel)
                        .where(PlanStepModel.plan_id == plan_id)
                        .order_by(PlanStepModel.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            plan = _plan_from_rows(row, steps)
            if plan.status == "cancelled":
                return PlanCancelTransition(plan, "already_cancelled")
            if plan.status in {"completed", "failed"}:
                return PlanCancelTransition(plan, "terminal_conflict")
            if active_run is None:
                active_run = await session.scalar(
                    _active_delegated_run_for_plan_query(
                        plan_id=plan_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                )
            if active_run is not None or any(step.status == "running" for step in plan.steps):
                return PlanCancelTransition(plan, "control_unsupported")

            for step in steps:
                if step.status in {"pending", "blocked"}:
                    step.status = "cancelled"
            row.status = "cancelled"
            row.current_step_id = None
            row.state_version += 1
            row.updated_at = now
            row.execution_claim_id = None
            row.execution_claim_step_id = None
            row.execution_claim_state_version = None
            row.execution_claim_key = None
            row.execution_claim_expires_at = None
            if formation_suppressed:
                row.formation_published_version = row.state_version
            metadata = loads(row.original_query, {})
            metadata.update(
                {
                    "next_action": None,
                    "state_version": row.state_version,
                    "formation_event_type": "cancel",
                }
            )
            row.original_query = dumps(metadata)
            return PlanCancelTransition(_plan_from_rows(row, steps), "cancelled")

    async def get_active_by_session(
        self, session_id: str, *, tenant_id: str, user_id: str
    ) -> Plan | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(PlanModel)
                .where(
                    PlanModel.session_id == session_id,
                    PlanModel.tenant_id == tenant_id,
                    PlanModel.user_id == user_id,
                    PlanModel.status.in_(["pending", "running", "blocked"]),
                )
                .order_by(desc(PlanModel.updated_at), desc(PlanModel.created_at))
                .limit(1)
            )
        return await self.get(row.plan_id, tenant_id=tenant_id, user_id=user_id) if row else None

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
        async with self.session_factory() as session:
            row = await session.scalar(
                select(PlanModel).where(
                    PlanModel.plan_id == plan_id,
                    PlanModel.tenant_id == tenant_id,
                    PlanModel.user_id == user_id,
                    PlanModel.status.in_(["pending", "running", "blocked"]),
                )
            )
            if row is None:
                return None
            expiry = _as_utc(row.execution_claim_expires_at)
            expired = bool(row.execution_claim_id and (expiry is None or expiry <= _as_utc(now)))
            allowed_step_statuses = ["pending", "blocked"]
            if expired and row.execution_claim_step_id == step_id:
                allowed_step_statuses.append("running")
            step = await session.scalar(
                select(PlanStepModel).where(
                    PlanStepModel.plan_id == plan_id,
                    PlanStepModel.step_id == step_id,
                    PlanStepModel.status.in_(allowed_step_statuses),
                )
            )
            if step is None:
                return None
            recovering = bool(
                expired and row.execution_claim_step_id == step_id and row.execution_claim_key
            )
            attempt = row.execution_attempt if recovering else row.execution_attempt + 1
            execution_key = (
                row.execution_claim_key if recovering else _execution_key(plan_id, step_id, attempt)
            )
            metadata = loads(row.original_query, {})
            metadata["next_action"] = None
            metadata["formation_event_type"] = "update"
            claimed = await session.execute(
                update(PlanModel)
                .where(
                    PlanModel.plan_id == plan_id,
                    PlanModel.tenant_id == tenant_id,
                    PlanModel.user_id == user_id,
                    PlanModel.status.in_(["pending", "running", "blocked"]),
                    or_(
                        PlanModel.execution_claim_id.is_(None),
                        PlanModel.execution_claim_expires_at.is_(None),
                        PlanModel.execution_claim_expires_at <= now,
                    ),
                )
                .values(
                    status="running",
                    current_step_id=step_id,
                    state_version=PlanModel.state_version + 1,
                    formation_published_version=(
                        PlanModel.state_version + 1
                        if formation_suppressed
                        else PlanModel.formation_published_version
                    ),
                    original_query=dumps(metadata),
                    updated_at=now,
                    execution_claim_id=claim_id,
                    execution_claim_step_id=step_id,
                    execution_claim_state_version=PlanModel.state_version + 1,
                    execution_claim_key=execution_key,
                    execution_attempt=attempt,
                    execution_claim_expires_at=lease_expires_at,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                await session.rollback()
                return None
            step_claimed = await session.execute(
                update(PlanStepModel)
                .where(
                    PlanStepModel.plan_id == plan_id,
                    PlanStepModel.step_id == step_id,
                    PlanStepModel.status.in_(allowed_step_statuses),
                )
                .values(status="running")
            )
            if step_claimed.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
        return await self.get(plan_id, tenant_id=tenant_id, user_id=user_id)

    async def save_claimed_step(
        self,
        plan: Plan,
        step_id: str,
        *,
        claim_id: str,
        formation_suppressed: bool = False,
    ) -> Plan | None:
        async with self.session_factory() as session:
            metadata = {
                "execution_policy": plan.execution_policy,
                "next_action": plan.next_action.model_dump(mode="json")
                if plan.next_action
                else None,
                "last_event_id": plan.last_event_id,
                "state_version": plan.state_version,
                "formation_event_type": plan.formation_event_type,
            }
            claimed = await session.execute(
                update(PlanModel)
                .where(
                    PlanModel.plan_id == plan.plan_id,
                    PlanModel.tenant_id == plan.tenant_id,
                    PlanModel.user_id == plan.user_id,
                    PlanModel.execution_claim_id == claim_id,
                    PlanModel.execution_claim_step_id == step_id,
                    PlanModel.execution_claim_state_version == plan.state_version - 1,
                    PlanModel.state_version == plan.state_version - 1,
                )
                .values(
                    session_id=plan.session_id or "",
                    status=plan.status,
                    current_step_id=plan.current_step_id,
                    state_version=plan.state_version,
                    formation_published_version=(
                        plan.state_version
                        if formation_suppressed
                        else PlanModel.formation_published_version
                    ),
                    original_query=dumps(metadata),
                    updated_at=plan.updated_at,
                    execution_claim_id=None,
                    execution_claim_step_id=None,
                    execution_claim_state_version=None,
                    execution_claim_key=None,
                    execution_claim_expires_at=None,
                )
            )
            if claimed.rowcount != 1:
                await session.rollback()
                return None
            await session.execute(
                delete(PlanStepModel).where(PlanStepModel.plan_id == plan.plan_id)
            )
            for step in plan.steps:
                session.add(
                    PlanStepModel(
                        step_id=step.step_id,
                        plan_id=plan.plan_id,
                        agent_id=step.agent_id,
                        status=step.status,
                        description=step.description,
                        depends_on_text=dumps(step.depends_on),
                        artifact_refs_text=dumps([ref.model_dump() for ref in step.artifact_refs]),
                    )
                )
            await session.commit()
        return await self.get(
            plan.plan_id,
            tenant_id=plan.tenant_id,
            user_id=plan.user_id,
        )

    async def list_formation_pending(self, *, limit: int = 100) -> list[Plan]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(PlanModel)
                        .where(PlanModel.formation_published_version < PlanModel.state_version)
                        .order_by(PlanModel.updated_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        values = []
        for row in rows:
            plan = await self.get(row.plan_id, tenant_id=row.tenant_id, user_id=row.user_id)
            if plan is not None:
                values.append(plan)
        return values

    async def mark_formation_published(self, plan_id: str, *, state_version: int) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(PlanModel)
                .where(
                    PlanModel.plan_id == plan_id,
                    PlanModel.formation_published_version < state_version,
                )
                .values(formation_published_version=state_version)
            )
            await session.commit()

    async def get_execution_claim_key(self, plan_id: str, *, claim_id: str) -> str | None:
        async with self.session_factory() as session:
            return await session.scalar(
                select(PlanModel.execution_claim_key).where(
                    PlanModel.plan_id == plan_id,
                    PlanModel.execution_claim_id == claim_id,
                )
            )

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
        async with self.session_factory() as session:
            renewed = await session.execute(
                update(PlanModel)
                .where(
                    PlanModel.plan_id == plan_id,
                    PlanModel.tenant_id == tenant_id,
                    PlanModel.user_id == user_id,
                    PlanModel.execution_claim_id == claim_id,
                    PlanModel.execution_claim_expires_at > now,
                )
                .values(execution_claim_expires_at=lease_expires_at)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return renewed.rowcount == 1


class DatabaseRouteLogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add(self, log: RouteLog) -> RouteLog:
        async with self.session_factory() as session:
            log = RouteLog.model_validate(log.model_dump(mode="python"))
            payload = log.model_dump()
            payload["candidate_agent_ids_text"] = dumps(payload.pop("candidate_agent_ids"))
            payload["evidence_text"] = dumps(payload.pop("evidence"))
            payload["raw_output_text"] = dumps(payload.pop("raw_output"))
            payload["parsed_output_text"] = dumps(payload.pop("parsed_output"))
            payload["error_text"] = dumps(payload.pop("error"))
            payload.pop("created_at", None)
            row = RouteLogModel(**payload)
            session.add(row)
            await session.commit()
            return log


def _agent_values(definition: AgentDefinition, *, source: str) -> dict:
    return {
        "agent_id": definition.agent_id,
        "name": definition.name,
        "description": definition.description,
        "version": definition.version,
        "revision": definition.revision,
        "type": definition.type,
        "enabled": definition.enabled,
        "domain": definition.domain,
        "capabilities_text": dumps(definition.capabilities),
        "tags_text": dumps(definition.tags),
        "trigger_text": dumps(definition.trigger.model_dump()),
        "access_policy_text": dumps(definition.access_policy.model_dump()),
        "required_inputs_text": dumps(definition.required_inputs),
        "optional_inputs_text": dumps(definition.optional_inputs),
        "input_schema_text": dumps(definition.input_schema.model_dump()),
        "output_schema_text": dumps(definition.output_schema.model_dump()),
        "invocation_text": dumps(definition.invocation.model_dump()),
        "ui_handoff_text": dumps(definition.ui_handoff.model_dump()),
        "context_text": dumps(definition.context.model_dump()),
        "priority": definition.priority,
        "metadata_text": dumps(definition.metadata),
        "source": source,
    }


def _mutation_audit(
    command: RegistryMutationCommand,
    *,
    before: AgentDefinition | None,
    after: AgentDefinition | None,
    revision: int,
) -> RegistryAuditRecord:
    return RegistryAuditRecord(
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


def _agent_from_row(row: AgentDefinitionModel) -> AgentDefinition:
    return AgentDefinition(
        agent_id=row.agent_id,
        name=row.name,
        description=row.description,
        version=row.version,
        revision=row.revision,
        enabled=row.enabled,
        type=row.type,
        domain=row.domain,
        capabilities=loads(row.capabilities_text, []),
        tags=loads(row.tags_text, []),
        trigger=loads(row.trigger_text, {}),
        access_policy=loads(row.access_policy_text, {}),
        required_inputs=loads(row.required_inputs_text, []),
        optional_inputs=loads(row.optional_inputs_text, []),
        input_schema=loads(row.input_schema_text, {}),
        output_schema=loads(row.output_schema_text, {}),
        invocation=loads(row.invocation_text, {}),
        ui_handoff=loads(row.ui_handoff_text, {}),
        context=loads(getattr(row, "context_text", "{}"), {}),
        priority=row.priority,
        metadata=loads(row.metadata_text, {}),
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_from_row(row: ChatMessageModel) -> ChatMessage:
    return ChatMessage(
        message_id=row.message_id,
        session_id=row.session_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        source=row.source,
        role=row.role,
        content=row.content,
        agent_id=row.agent_id,
        agent_session_id=row.agent_session_id,
        request_id=row.request_id,
        event_id=row.event_id,
        metadata=loads(row.metadata_text, {}),
        created_at=row.created_at,
    )


def _conversation_event_from_row(row: ConversationEventModel) -> ConversationEvent:
    return ConversationEvent(
        event_id=row.event_id,
        session_id=row.session_id,
        request_id=row.request_id,
        user_id=row.user_id,
        event_type=row.event_type,
        source=row.source,
        agent_id=row.agent_id,
        payload=loads(row.payload_text, {}),
        created_at=row.created_at,
    )


def _agent_event_from_row(row: AgentEventModel) -> AgentEvent:
    return AgentEvent(
        event_id=row.event_id,
        run_id=row.run_id,
        request_id=row.request_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        turn_id=row.turn_id,
        agent_session_id=row.agent_session_id,
        event_type=row.event_type,
        status=row.status,
        plan_id=row.plan_id,
        step_id=row.step_id,
        sequence=row.sequence,
        run_state_version=row.run_state_version,
        payload=loads(row.payload_text, {}),
        created_at=row.created_at,
    )


async def _lock_plan_row(
    session: AsyncSession,
    *,
    plan_id: str,
    tenant_id: str,
    user_id: str,
) -> PlanModel | None:
    """Serialize Plan decisions on PostgreSQL and SQLite without a lock table."""
    locked = await session.execute(
        update(PlanModel)
        .where(
            PlanModel.plan_id == plan_id,
            PlanModel.tenant_id == tenant_id,
            PlanModel.user_id == user_id,
        )
        .values(updated_at=PlanModel.updated_at)
        .execution_options(synchronize_session=False)
    )
    if locked.rowcount != 1:
        return None
    return await session.scalar(
        select(PlanModel).where(
            PlanModel.plan_id == plan_id,
            PlanModel.tenant_id == tenant_id,
            PlanModel.user_id == user_id,
        )
    )


async def _lock_active_delegated_run_for_plan(
    session: AsyncSession,
    *,
    plan_id: str,
    tenant_id: str,
    user_id: str,
) -> AgentRunModel | None:
    return await session.scalar(
        _active_delegated_run_for_plan_query(
            plan_id=plan_id,
            tenant_id=tenant_id,
            user_id=user_id,
        ).with_for_update()
    )


def _active_delegated_run_for_plan_query(
    *,
    plan_id: str,
    tenant_id: str,
    user_id: str,
):
    return (
        select(AgentRunModel)
        .where(
            AgentRunModel.plan_id == plan_id,
            AgentRunModel.tenant_id == tenant_id,
            AgentRunModel.user_id == user_id,
            AgentRunModel.delegated.is_(True),
            AgentRunModel.status.in_(["pending", "running", "blocked"]),
        )
        .order_by(AgentRunModel.created_at, AgentRunModel.run_id)
        .limit(1)
    )


def _plan_from_rows(row: PlanModel, steps: list[PlanStepModel]) -> Plan:
    metadata = loads(row.original_query, {})
    return Plan(
        plan_id=row.plan_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        status=row.status,
        current_step_id=row.current_step_id,
        execution_policy=metadata.get("execution_policy"),
        next_action=metadata.get("next_action"),
        last_event_id=metadata.get("last_event_id"),
        state_version=row.state_version,
        updated_at=_as_utc(row.updated_at),
        formation_event_type=metadata.get("formation_event_type", "update"),
        steps=[
            PlanStep(
                step_id=step.step_id,
                agent_id=step.agent_id,
                status=step.status,
                description=step.description,
                depends_on=loads(step.depends_on_text, []),
                artifact_refs=loads(step.artifact_refs_text, []),
            )
            for step in steps
        ],
    )


def _run_values(run: AgentRun) -> dict:
    run = AgentRun.model_validate(run.model_dump(mode="python"))
    return {
        "run_id": run.run_id,
        "request_id": run.request_id,
        "session_id": run.session_id,
        "agent_id": run.agent_id,
        "user_id": run.user_id,
        "tenant_id": run.tenant_id,
        "turn_id": run.turn_id,
        "plan_id": run.plan_id,
        "step_id": run.step_id,
        "status": run.status,
        "invoker_type": run.invoker_type,
        "agent_revision": run.agent_revision,
        "handling_kind": run.handling_kind,
        "binding_snapshot_text": (
            dumps(run.binding_snapshot.model_dump(mode="json")) if run.binding_snapshot else None
        ),
        "delegated": run.delegated,
        "delegation_key": run.delegation_key,
        "state_version": run.state_version,
        "event_sequence": run.event_sequence,
        "deadline_at": run.deadline_at,
        "heartbeat_at": run.heartbeat_at,
        "claim_owner": run.claim_owner,
        "claim_token": run.claim_token,
        "claim_expires_at": run.claim_expires_at,
        "terminal_event_id": run.terminal_event_id,
        "input_text": dumps(run.input),
        "output_text": dumps(run.output),
        "error_text": dumps(run.error),
        "latency_ms": run.latency_ms,
        "formation_suppressed": run.formation_suppressed,
        "used_memory_ids_text": dumps(run.used_memory_ids),
    }


def _run_from_row(row: AgentRunModel) -> AgentRun:
    return AgentRun(
        run_id=row.run_id,
        request_id=row.request_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        turn_id=row.turn_id,
        plan_id=row.plan_id,
        step_id=row.step_id,
        status=row.status,
        invoker_type=row.invoker_type,
        agent_revision=row.agent_revision,
        handling_kind=row.handling_kind,
        binding_snapshot=loads(row.binding_snapshot_text, None),
        delegated=row.delegated,
        delegation_key=row.delegation_key,
        state_version=row.state_version,
        event_sequence=row.event_sequence,
        deadline_at=row.deadline_at,
        heartbeat_at=row.heartbeat_at,
        claim_owner=row.claim_owner,
        claim_token=row.claim_token,
        claim_expires_at=row.claim_expires_at,
        terminal_event_id=row.terminal_event_id,
        input=loads(row.input_text, {}),
        output=loads(row.output_text, None),
        error=loads(row.error_text, None),
        latency_ms=row.latency_ms,
        formation_suppressed=row.formation_suppressed,
        used_memory_ids=loads(row.used_memory_ids_text, []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_run_identity(existing: AgentRun, incoming: AgentRun) -> None:
    identity_fields = (
        "request_id",
        "session_id",
        "agent_id",
        "user_id",
        "tenant_id",
        "turn_id",
        "plan_id",
        "step_id",
        "agent_revision",
        "handling_kind",
        "binding_snapshot",
    )
    if any(getattr(existing, field) != getattr(incoming, field) for field in identity_fields):
        raise ValueError("Agent run identity cannot be changed")


def _result_values(result: AgentResult) -> dict:
    return {
        "result_id": result.result_id or f"result_{uuid4().hex}",
        "run_id": result.run_id,
        "session_id": result.session_id,
        "agent_id": result.agent_id,
        "user_id": result.user_id,
        "tenant_id": result.tenant_id,
        "turn_id": result.turn_id,
        "plan_id": result.plan_id,
        "step_id": result.step_id,
        "status": result.status,
        "run_state_version": result.run_state_version,
        "message": result.message,
        "formation_suppressed": result.formation_suppressed,
        "turn_captured": (result.formation_suppressed and not result.formation_skip_audit_required),
        "output_text": dumps(result.output),
        "artifact_refs_text": dumps(result.artifact_refs),
        "error_text": dumps(result.error),
    }


def _result_from_row(row: AgentResultModel) -> AgentResult:
    output = loads(row.output_text, None)
    return AgentResult(
        result_id=row.result_id,
        run_id=row.run_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        turn_id=row.turn_id,
        plan_id=row.plan_id,
        step_id=row.step_id,
        status=row.status,
        run_state_version=row.run_state_version,
        message=row.message,
        formation_suppressed=row.formation_suppressed,
        formation_skip_audit_required=(row.formation_suppressed and not row.turn_captured),
        output=output,
        artifact_refs=loads(row.artifact_refs_text, []),
        error=loads(row.error_text, None),
        created_at=row.created_at,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _execution_key(plan_id: str, step_id: str, attempt: int) -> str:
    identity = "\x1f".join((plan_id, step_id, str(attempt)))
    return f"plan_exec_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
