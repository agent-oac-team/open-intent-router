import asyncio
from datetime import UTC
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import RegistryRevisionModel
from app.repositories.json_utils import dumps, loads
from app.schemas.registry_audit import RegistryAuditRecord


class RegistryAuditStore(Protocol):
    async def append(self, record: RegistryAuditRecord) -> RegistryAuditRecord: ...

    async def list_for_agent(self, agent_id: str) -> list[RegistryAuditRecord]: ...


class MemoryRegistryAuditStore:
    def __init__(self) -> None:
        self.records: list[RegistryAuditRecord] = []
        self._lock = asyncio.Lock()

    async def append(self, record: RegistryAuditRecord) -> RegistryAuditRecord:
        async with self._lock:
            self.records.append(record)
        return record

    async def list_for_agent(self, agent_id: str) -> list[RegistryAuditRecord]:
        return [record for record in self.records if record.agent_id == agent_id]


class DatabaseRegistryAuditStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def append(self, record: RegistryAuditRecord) -> RegistryAuditRecord:
        async with self.session_factory() as session, session.begin():
            session.add(
                RegistryRevisionModel(
                    revision_id=record.revision_id,
                    agent_id=record.agent_id,
                    revision=record.revision,
                    operation=record.operation,
                    operator_id=record.operator_id,
                    source=record.source,
                    before_text=dumps(record.before) if record.before is not None else None,
                    after_text=dumps(record.after) if record.after is not None else None,
                    created_at=record.created_at,
                )
            )
        return record

    async def list_for_agent(self, agent_id: str) -> list[RegistryAuditRecord]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(RegistryRevisionModel)
                        .where(RegistryRevisionModel.agent_id == agent_id)
                        .order_by(RegistryRevisionModel.revision)
                    )
                )
                .scalars()
                .all()
            )
            return [_from_row(row) for row in rows]


def _from_row(row: RegistryRevisionModel) -> RegistryAuditRecord:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return RegistryAuditRecord(
        revision_id=row.revision_id,
        agent_id=row.agent_id,
        revision=row.revision,
        operation=row.operation,
        operator_id=row.operator_id,
        source=row.source,
        before=loads(row.before_text, None),
        after=loads(row.after_text, None),
        created_at=created_at,
    )
