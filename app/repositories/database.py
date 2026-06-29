from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    AgentDefinitionModel,
    AgentEventModel,
    AgentResultModel,
    AgentRunModel,
    ChatMessageModel,
    ConversationEventModel,
    PlanModel,
    PlanStepModel,
    RouteLogModel,
)
from app.repositories.json_utils import dumps, loads
from app.schemas.agents import AgentDefinition
from app.schemas.events import AgentEvent, ConversationEvent
from app.schemas.logs import AgentResult, AgentRun, RouteLog
from app.schemas.plans import Plan, PlanStep
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

    async def upsert(self, definition: AgentDefinition) -> AgentDefinition:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentDefinitionModel).where(
                    AgentDefinitionModel.agent_id == definition.agent_id
                )
            )
            values = _agent_values(definition, source="database")
            if row is None:
                row = AgentDefinitionModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _agent_from_row(row)

    async def set_enabled(self, agent_id: str, enabled: bool) -> AgentDefinition | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == agent_id)
            )
            if row is None:
                return None
            row.enabled = enabled
            await session.commit()
            await session.refresh(row)
            return _agent_from_row(row)

    async def delete(self, agent_id: str) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                delete(AgentDefinitionModel).where(AgentDefinitionModel.agent_id == agent_id)
            )
            await session.commit()
            return bool(result.rowcount)


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
        source: str | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[ChatMessage]:
        async with self.session_factory() as session:
            stmt = select(ChatMessageModel).where(ChatMessageModel.session_id == session_id)
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
            values = _run_values(run)
            if row is None:
                row = AgentRunModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _run_from_row(row)

    async def get_run(self, run_id: str) -> AgentRun | None:
        async with self.session_factory() as session:
            row = await session.get(AgentRunModel, run_id)
            return _run_from_row(row) if row else None


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

    async def list_recent(self, session_id: str, *, limit: int = 5) -> list[AgentResult]:
        async with self.session_factory() as session:
            stmt = (
                select(AgentResultModel)
                .where(AgentResultModel.session_id == session_id)
                .order_by(desc(AgentResultModel.created_at))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_result_from_row(row) for row in rows]


class DatabasePlanRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def save(self, plan: Plan) -> Plan:
        async with self.session_factory() as session:
            row = await session.get(PlanModel, plan.plan_id)
            metadata = {
                "execution_policy": plan.execution_policy,
                "next_action": plan.next_action.model_dump(mode="json") if plan.next_action else None,
            }
            values = {
                "plan_id": plan.plan_id,
                "session_id": plan.session_id or "",
                "status": plan.status,
                "current_step_id": plan.current_step_id,
                "original_query": dumps(metadata),
            }
            if row is None:
                row = PlanModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.execute(delete(PlanStepModel).where(PlanStepModel.plan_id == plan.plan_id))
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

    async def get(self, plan_id: str) -> Plan | None:
        async with self.session_factory() as session:
            row = await session.get(PlanModel, plan_id)
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
            metadata = loads(row.original_query, {})
            return Plan(
                plan_id=row.plan_id,
                session_id=row.session_id,
                status=row.status,
                current_step_id=row.current_step_id,
                execution_policy=metadata.get("execution_policy"),
                next_action=metadata.get("next_action"),
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


class DatabaseRouteLogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add(self, log: RouteLog) -> RouteLog:
        async with self.session_factory() as session:
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
        "priority": definition.priority,
        "metadata_text": dumps(definition.metadata),
        "source": source,
    }


def _agent_from_row(row: AgentDefinitionModel) -> AgentDefinition:
    return AgentDefinition(
        agent_id=row.agent_id,
        name=row.name,
        description=row.description,
        version=row.version,
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
        agent_session_id=row.agent_session_id,
        event_type=row.event_type,
        status=row.status,
        plan_id=row.plan_id,
        step_id=row.step_id,
        payload=loads(row.payload_text, {}),
        created_at=row.created_at,
    )


def _run_values(run: AgentRun) -> dict:
    return {
        "run_id": run.run_id,
        "request_id": run.request_id,
        "session_id": run.session_id,
        "agent_id": run.agent_id,
        "status": run.status,
        "invoker_type": run.invoker_type,
        "input_text": dumps(run.input),
        "output_text": dumps(run.output),
        "error_text": dumps(run.error),
        "latency_ms": run.latency_ms,
    }


def _run_from_row(row: AgentRunModel) -> AgentRun:
    return AgentRun(
        run_id=row.run_id,
        request_id=row.request_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        status=row.status,
        invoker_type=row.invoker_type,
        input=loads(row.input_text, {}),
        output=loads(row.output_text, None),
        error=loads(row.error_text, None),
        latency_ms=row.latency_ms,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_values(result: AgentResult) -> dict:
    output = result.output or {}
    if result.plan_id or result.step_id:
        output = {
            **output,
            "_execution": {
                "plan_id": result.plan_id,
                "step_id": result.step_id,
            },
        }
    return {
        "result_id": result.result_id or f"result_{uuid4().hex}",
        "run_id": result.run_id,
        "session_id": result.session_id,
        "agent_id": result.agent_id,
        "status": result.status,
        "output_text": dumps(output),
        "artifact_refs_text": dumps(result.artifact_refs),
        "error_text": dumps(result.error),
    }


def _result_from_row(row: AgentResultModel) -> AgentResult:
    output = loads(row.output_text, None)
    execution = output.get("_execution", {}) if isinstance(output, dict) else {}
    return AgentResult(
        result_id=row.result_id,
        run_id=row.run_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        plan_id=execution.get("plan_id"),
        step_id=execution.get("step_id"),
        status=row.status,
        output=output,
        artifact_refs=loads(row.artifact_refs_text, []),
        error=loads(row.error_text, None),
        created_at=row.created_at,
    )
