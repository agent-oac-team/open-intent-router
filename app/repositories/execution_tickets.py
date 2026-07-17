import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ExecutionTicketModel
from app.repositories.json_utils import dumps, loads
from app.schemas.execution_tickets import (
    ExecutionTicketClaims,
    ExecutionTicketRecord,
    ExecutionTicketStatus,
    LegacyExecutionCorrelationQuery,
)


class ExecutionTicketConflict(ValueError):
    pass


class ExecutionTicketStore(Protocol):
    async def put(self, record: ExecutionTicketRecord) -> None: ...

    async def get(self, ticket_hash: str) -> ExecutionTicketRecord | None: ...

    async def update(self, record: ExecutionTicketRecord) -> ExecutionTicketRecord: ...

    async def find_active(
        self, query: LegacyExecutionCorrelationQuery
    ) -> list[ExecutionTicketRecord]: ...


class MemoryExecutionTicketStore:
    def __init__(self) -> None:
        self.records: dict[str, ExecutionTicketRecord] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: ExecutionTicketRecord) -> None:
        async with self._lock:
            if record.ticket_hash in self.records:
                raise ExecutionTicketConflict("Execution Ticket already exists")
            self.records[record.ticket_hash] = record

    async def get(self, ticket_hash: str) -> ExecutionTicketRecord | None:
        async with self._lock:
            record = self.records.get(ticket_hash)
            return record.model_copy(deep=True) if record else None

    async def update(self, record: ExecutionTicketRecord) -> ExecutionTicketRecord:
        async with self._lock:
            if record.ticket_hash not in self.records:
                raise ExecutionTicketConflict("Execution Ticket not found")
            self.records[record.ticket_hash] = record
            return record.model_copy(deep=True)

    async def find_active(
        self, query: LegacyExecutionCorrelationQuery
    ) -> list[ExecutionTicketRecord]:
        async with self._lock:
            return [
                record.model_copy(deep=True)
                for record in self.records.values()
                if _matches_legacy(record, query)
            ]


class DatabaseExecutionTicketStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def put(self, record: ExecutionTicketRecord) -> None:
        async with self.session_factory() as session, session.begin():
            if await session.get(ExecutionTicketModel, record.ticket_hash):
                raise ExecutionTicketConflict("Execution Ticket already exists")
            session.add(
                ExecutionTicketModel(
                    ticket_hash=record.ticket_hash,
                    request_id=record.claims.request_id,
                    run_id=record.claims.run_id,
                    turn_id=record.claims.turn_id,
                    tenant_id=record.claims.tenant_id,
                    user_id=record.claims.user_id,
                    agent_id=record.claims.agent_id,
                    plan_id=record.claims.plan_id,
                    step_id=record.claims.step_id,
                    purpose=record.claims.purpose,
                    claims_text=dumps(record.claims.model_dump(mode="json")),
                    status=record.status.value,
                    run_state_version=record.run_state_version,
                    event_sequence=record.event_sequence,
                    lease_owner=record.lease_owner,
                    lease_token=record.lease_token,
                    lease_expires_at=record.lease_expires_at,
                    consumed_event_id=record.consumed_event_id,
                    consumed_at=record.consumed_at,
                )
            )

    async def get(self, ticket_hash: str) -> ExecutionTicketRecord | None:
        async with self.session_factory() as session:
            row = await session.get(ExecutionTicketModel, ticket_hash)
            return _record_from_row(row) if row else None

    async def update(self, record: ExecutionTicketRecord) -> ExecutionTicketRecord:
        async with self.session_factory() as session, session.begin():
            row = await session.get(ExecutionTicketModel, record.ticket_hash, with_for_update=True)
            if row is None:
                raise ExecutionTicketConflict("Execution Ticket not found")
            row.status = record.status.value
            row.run_state_version = record.run_state_version
            row.event_sequence = record.event_sequence
            row.lease_owner = record.lease_owner
            row.lease_token = record.lease_token
            row.lease_expires_at = record.lease_expires_at
            row.consumed_event_id = record.consumed_event_id
            row.consumed_at = record.consumed_at
            return _record_from_row(row)

    async def find_active(
        self, query: LegacyExecutionCorrelationQuery
    ) -> list[ExecutionTicketRecord]:
        async with self.session_factory() as session:
            conditions = [
                ExecutionTicketModel.tenant_id == query.tenant_id,
                ExecutionTicketModel.user_id == query.user_id,
                ExecutionTicketModel.agent_id == query.agent_id,
                ExecutionTicketModel.plan_id == query.plan_id,
                ExecutionTicketModel.step_id == query.step_id,
                ExecutionTicketModel.purpose == query.purpose,
                ExecutionTicketModel.status.in_(("issued", "claimed")),
            ]
            if query.request_id:
                conditions.append(ExecutionTicketModel.request_id == query.request_id)
            rows = (
                (await session.execute(select(ExecutionTicketModel).where(*conditions)))
                .scalars()
                .all()
            )
            return [
                record
                for row in rows
                if (record := _record_from_row(row)).claims.expires_at > _as_utc(query.now)
            ]


def ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def new_nonce() -> str:
    return uuid4().hex + uuid4().hex


def _record_from_row(row: ExecutionTicketModel) -> ExecutionTicketRecord:
    claims = ExecutionTicketClaims.model_validate(loads(row.claims_text, {}))
    return ExecutionTicketRecord(
        ticket_hash=row.ticket_hash,
        claims=claims,
        status=ExecutionTicketStatus(row.status),
        run_state_version=row.run_state_version,
        event_sequence=row.event_sequence,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=_as_utc(row.lease_expires_at),
        consumed_event_id=row.consumed_event_id,
        consumed_at=_as_utc(row.consumed_at),
    )


def _matches_legacy(record: ExecutionTicketRecord, query: LegacyExecutionCorrelationQuery) -> bool:
    claims = record.claims
    return (
        record.status in {ExecutionTicketStatus.ISSUED, ExecutionTicketStatus.CLAIMED}
        and claims.expires_at > _as_utc(query.now)
        and claims.tenant_id == query.tenant_id
        and claims.user_id == query.user_id
        and (query.request_id is None or claims.request_id == query.request_id)
        and claims.agent_id == query.agent_id
        and claims.plan_id == query.plan_id
        and claims.step_id == query.step_id
        and claims.purpose == query.purpose
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
