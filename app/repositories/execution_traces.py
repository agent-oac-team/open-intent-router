import asyncio
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ExecutionTraceEventModel
from app.repositories.json_utils import dumps, loads
from app.schemas.execution_traces import (
    ExecutionTraceEvent,
    ExecutionTraceEventDraft,
    ExecutionTraceQuery,
)


class ExecutionTraceSourceConflict(ValueError):
    pass


@runtime_checkable
class ExecutionTraceRepository(Protocol):
    async def append(self, event: ExecutionTraceEventDraft) -> tuple[ExecutionTraceEvent, bool]: ...

    async def list_events(
        self, query: ExecutionTraceQuery, *, after_offset: int = 0
    ) -> list[ExecutionTraceEvent]: ...


class MemoryExecutionTraceRepository:
    def __init__(self) -> None:
        self._events: list[ExecutionTraceEvent] = []
        self._by_source: dict[tuple[str, str, int], ExecutionTraceEvent] = {}
        self._next_offset = 1
        self._lock = asyncio.Lock()

    async def append(self, event: ExecutionTraceEventDraft) -> tuple[ExecutionTraceEvent, bool]:
        event = ExecutionTraceEventDraft.model_validate(event.model_dump())
        source_key = (event.source, event.source_event_id, event.source_version)
        async with self._lock:
            existing = self._by_source.get(source_key)
            if existing is not None:
                if not _is_idempotent_replay(existing, event):
                    raise ExecutionTraceSourceConflict("execution trace source identity conflict")
                return existing.model_copy(deep=True), False
            stored = ExecutionTraceEvent(
                **event.model_dump(),
                event_offset=self._next_offset,
            )
            self._next_offset += 1
            self._events.append(stored)
            self._by_source[source_key] = stored
            return stored.model_copy(deep=True), True

    async def list_events(
        self, query: ExecutionTraceQuery, *, after_offset: int = 0
    ) -> list[ExecutionTraceEvent]:
        async with self._lock:
            events = [
                event.model_copy(deep=True)
                for event in self._events
                if event.event_offset > after_offset
                and event.tenant_id == query.tenant_id
                and event.user_id == query.user_id
                and event.session_id == query.session_id
                and event.turn_id == query.turn_id
            ]
        return events


class DatabaseExecutionTraceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def append(self, event: ExecutionTraceEventDraft) -> tuple[ExecutionTraceEvent, bool]:
        event = ExecutionTraceEventDraft.model_validate(event.model_dump())
        try:
            async with self.session_factory() as session:
                row = ExecutionTraceEventModel(**_trace_values(event))
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return _event_from_row(row), True
        except IntegrityError:
            async with self.session_factory() as session:
                row = await session.scalar(
                    select(ExecutionTraceEventModel).where(
                        ExecutionTraceEventModel.source == event.source,
                        ExecutionTraceEventModel.source_event_id == event.source_event_id,
                        ExecutionTraceEventModel.source_version == event.source_version,
                    )
                )
                if row is None:
                    raise
                stored = _event_from_row(row)
                if not _is_idempotent_replay(stored, event):
                    raise ExecutionTraceSourceConflict(
                        "execution trace source identity conflict"
                    ) from None
                return stored, False

    async def list_events(
        self, query: ExecutionTraceQuery, *, after_offset: int = 0
    ) -> list[ExecutionTraceEvent]:
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(ExecutionTraceEventModel)
                .where(
                    ExecutionTraceEventModel.tenant_id == query.tenant_id,
                    ExecutionTraceEventModel.user_id == query.user_id,
                    ExecutionTraceEventModel.session_id == query.session_id,
                    ExecutionTraceEventModel.turn_id == query.turn_id,
                    ExecutionTraceEventModel.event_offset > after_offset,
                )
                .order_by(ExecutionTraceEventModel.event_offset)
            )
            return [_event_from_row(row) for row in rows]


def _event_identity(event: ExecutionTraceEvent) -> dict:
    return event.model_dump(
        mode="json",
        exclude={"event_offset", "recorded_at"},
    )


def _draft_identity(event: ExecutionTraceEventDraft) -> dict:
    return event.model_dump(mode="json")


def _is_idempotent_replay(
    stored: ExecutionTraceEvent,
    incoming: ExecutionTraceEventDraft,
) -> bool:
    stored_identity = _event_identity(stored)
    incoming_identity = _draft_identity(incoming)
    if stored.schema_version == 1 and incoming.schema_version >= 2:
        stored_identity["schema_version"] = incoming.schema_version
        if stored.event_type == "memory_decision":
            stored_identity["facts"] = {
                key: value
                for key, value in stored_identity["facts"].items()
                if key not in {"previous_value", "proposed_value"}
            }
    return stored_identity == incoming_identity


def _trace_values(event: ExecutionTraceEventDraft) -> dict:
    return {
        "trace_id": event.trace_id,
        "tenant_id": event.tenant_id,
        "user_id": event.user_id,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "run_id": event.run_id,
        "event_type": event.event_type,
        "stage": event.stage,
        "status": event.status,
        "source": event.source,
        "source_event_id": event.source_event_id,
        "source_version": event.source_version,
        "reason_code": event.reason_code,
        "facts_text": dumps(event.facts),
        "evidence_refs_text": dumps(
            [reference.model_dump(mode="json") for reference in event.evidence_refs]
        ),
        "visibility": event.visibility,
        "schema_version": event.schema_version,
        "occurred_at": event.occurred_at,
    }


def _event_from_row(row: ExecutionTraceEventModel) -> ExecutionTraceEvent:
    return ExecutionTraceEvent.model_validate(
        {
            "event_offset": row.event_offset,
            "trace_id": row.trace_id,
            "tenant_id": row.tenant_id,
            "user_id": row.user_id,
            "session_id": row.session_id,
            "turn_id": row.turn_id,
            "run_id": row.run_id,
            "event_type": row.event_type,
            "stage": row.stage,
            "status": row.status,
            "source": row.source,
            "source_event_id": row.source_event_id,
            "source_version": row.source_version,
            "reason_code": row.reason_code,
            "facts": loads(row.facts_text, {}),
            "evidence_refs": loads(row.evidence_refs_text, []),
            "visibility": row.visibility,
            "schema_version": row.schema_version,
            "occurred_at": row.occurred_at,
            "recorded_at": row.recorded_at,
        }
    )
