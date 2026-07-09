from datetime import UTC, datetime

from sqlalchemy import delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    KnowledgeChunkModel,
    KnowledgeRetrievalLogModel,
    KnowledgeSourceModel,
    MemoryEventModel,
    MemoryItemModel,
)
from app.repositories.json_utils import dumps, loads
from app.schemas.knowledge import KnowledgeChunk, KnowledgeRetrievalLog, KnowledgeSource
from app.schemas.memory import MemoryEvent, MemoryItem


class MemoryItemRepository:
    def __init__(self) -> None:
        self.items: dict[str, MemoryItem] = {}
        self.events: list[MemoryEvent] = []

    async def add(self, item: MemoryItem) -> MemoryItem:
        self.items[item.memory_id] = item
        return item

    async def list_active(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        scopes: list[str] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        now = datetime.now(UTC)
        values = list(self.items.values())
        if user_id:
            values = [item for item in values if item.user_id in {None, user_id}]
        if tenant_id:
            values = [item for item in values if item.tenant_id in {None, tenant_id}]
        if scopes:
            scope_set = set(scopes)
            values = [item for item in values if str(item.scope) in scope_set]
        if subject_type:
            values = [item for item in values if item.subject_type == subject_type]
        if subject_id:
            values = [item for item in values if item.subject_id == subject_id]
        if agent_id:
            values = [item for item in values if item.agent_id in {None, agent_id}]
        values = [
            item for item in values if item.ttl_expires_at is None or item.ttl_expires_at > now
        ]
        return sorted(values, key=lambda item: item.updated_at, reverse=True)[:limit]

    async def add_event(self, event: MemoryEvent) -> MemoryEvent:
        self.events.append(event)
        return event

    async def list_events(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEvent]:
        values = list(self.events)
        if user_id:
            values = [event for event in values if event.user_id in {None, user_id}]
        if tenant_id:
            values = [event for event in values if event.tenant_id in {None, tenant_id}]
        if agent_id:
            values = [event for event in values if event.agent_id in {None, agent_id}]
        return sorted(values, key=lambda event: event.created_at, reverse=True)[:limit]

    async def expire_before(self, when: datetime | None = None) -> list[str]:
        now = when or datetime.now(UTC)
        expired = [
            memory_id
            for memory_id, item in self.items.items()
            if item.ttl_expires_at is not None and item.ttl_expires_at <= now
        ]
        for memory_id in expired:
            self.items.pop(memory_id, None)
        return expired

    async def list_expired(self, when: datetime | None = None) -> list[MemoryItem]:
        now = when or datetime.now(UTC)
        return [
            item
            for item in self.items.values()
            if item.ttl_expires_at is not None and item.ttl_expires_at <= now
        ]


class KnowledgeRepository:
    def __init__(self) -> None:
        self.sources: dict[str, KnowledgeSource] = {}
        self.chunks: dict[str, KnowledgeChunk] = {}
        self.logs: list[KnowledgeRetrievalLog] = []

    async def upsert_source(self, source: KnowledgeSource) -> KnowledgeSource:
        self.sources[source.source_id] = source
        return source

    async def get_sources(self, source_ids: list[str] | None = None) -> list[KnowledgeSource]:
        if source_ids:
            return [source for sid in source_ids if (source := self.sources.get(sid))]
        return list(self.sources.values())

    async def add_chunk(self, chunk: KnowledgeChunk) -> KnowledgeChunk:
        self.chunks[chunk.chunk_id] = chunk
        if chunk.source_id not in self.sources:
            self.sources[chunk.source_id] = KnowledgeSource(
                source_id=chunk.source_id,
                name=chunk.source_id,
            )
        return chunk

    async def search_chunks(
        self,
        *,
        query: str,
        source_ids: list[str],
        limit: int,
    ) -> list[tuple[KnowledgeChunk, float]]:
        query_tokens = _tokens(query)
        scored: list[tuple[KnowledgeChunk, float]] = []
        for chunk in self.chunks.values():
            if source_ids and chunk.source_id not in source_ids:
                continue
            content_tokens = _tokens(chunk.content)
            overlap = len(query_tokens & content_tokens)
            score = overlap / max(len(query_tokens), 1)
            if query_tokens and overlap == 0:
                continue
            scored.append((chunk, min(score or 0.1, 1.0)))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]

    async def add_log(self, log: KnowledgeRetrievalLog) -> KnowledgeRetrievalLog:
        self.logs.append(log)
        return log

    async def list_chunks(
        self,
        *,
        source_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[KnowledgeChunk]:
        values = list(self.chunks.values())
        if source_ids:
            source_set = set(source_ids)
            values = [chunk for chunk in values if chunk.source_id in source_set]
        return sorted(values, key=lambda chunk: chunk.updated_at, reverse=True)[:limit]

    async def list_logs(
        self,
        *,
        caller_type: str | None = None,
        caller_id: str | None = None,
        purpose: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeRetrievalLog]:
        values = list(self.logs)
        if caller_type:
            values = [log for log in values if log.caller_type == caller_type]
        if caller_id:
            values = [log for log in values if log.caller_id == caller_id]
        if purpose:
            values = [log for log in values if log.purpose == purpose]
        if tenant_id:
            values = [log for log in values if log.tenant_id in {None, tenant_id}]
        return sorted(values, key=lambda log: log.created_at, reverse=True)[:limit]


def _tokens(value: str) -> set[str]:
    return {part.strip().lower() for part in value.split() if part.strip()}


class DatabaseMemoryItemRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add(self, item: MemoryItem) -> MemoryItem:
        async with self.session_factory() as session:
            row = await session.get(MemoryItemModel, item.memory_id)
            values = _memory_values(item)
            if row is None:
                row = MemoryItemModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _memory_from_row(row)

    async def list_active(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        scopes: list[str] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        async with self.session_factory() as session:
            stmt = select(MemoryItemModel)
            if user_id:
                stmt = stmt.where(
                    or_(MemoryItemModel.user_id.is_(None), MemoryItemModel.user_id == user_id)
                )
            if tenant_id:
                stmt = stmt.where(
                    or_(MemoryItemModel.tenant_id.is_(None), MemoryItemModel.tenant_id == tenant_id)
                )
            if scopes:
                stmt = stmt.where(MemoryItemModel.scope.in_(scopes))
            if subject_type:
                stmt = stmt.where(MemoryItemModel.subject_type == subject_type)
            if subject_id:
                stmt = stmt.where(MemoryItemModel.subject_id == subject_id)
            if agent_id:
                stmt = stmt.where(
                    or_(MemoryItemModel.agent_id.is_(None), MemoryItemModel.agent_id == agent_id)
                )
            stmt = stmt.order_by(desc(MemoryItemModel.updated_at))
            rows = (await session.execute(stmt)).scalars().all()
            return [
                item for item in (_memory_from_row(row) for row in rows) if _memory_active(item)
            ][:limit]

    async def add_event(self, event: MemoryEvent) -> MemoryEvent:
        async with self.session_factory() as session:
            row = MemoryEventModel(**_memory_event_values(event))
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _memory_event_from_row(row)

    async def list_events(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEvent]:
        async with self.session_factory() as session:
            stmt = select(MemoryEventModel)
            if user_id:
                stmt = stmt.where(
                    or_(MemoryEventModel.user_id.is_(None), MemoryEventModel.user_id == user_id)
                )
            if tenant_id:
                stmt = stmt.where(
                    or_(
                        MemoryEventModel.tenant_id.is_(None),
                        MemoryEventModel.tenant_id == tenant_id,
                    )
                )
            if agent_id:
                stmt = stmt.where(
                    or_(MemoryEventModel.agent_id.is_(None), MemoryEventModel.agent_id == agent_id)
                )
            stmt = stmt.order_by(desc(MemoryEventModel.created_at)).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_memory_event_from_row(row) for row in rows]

    async def expire_before(self, when: datetime | None = None) -> list[str]:
        now = when or datetime.now(UTC)
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryItemModel.memory_id).where(
                            MemoryItemModel.ttl_expires_at.is_not(None),
                            MemoryItemModel.ttl_expires_at <= now,
                        )
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                delete(MemoryItemModel).where(MemoryItemModel.memory_id.in_(rows))
            )
            await session.commit()
            return list(rows)

    async def list_expired(self, when: datetime | None = None) -> list[MemoryItem]:
        now = when or datetime.now(UTC)
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryItemModel).where(
                            MemoryItemModel.ttl_expires_at.is_not(None),
                            MemoryItemModel.ttl_expires_at <= now,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_memory_from_row(row) for row in rows]


class DatabaseKnowledgeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def upsert_source(self, source: KnowledgeSource) -> KnowledgeSource:
        async with self.session_factory() as session:
            row = await session.get(KnowledgeSourceModel, source.source_id)
            values = _knowledge_source_values(source)
            if row is None:
                row = KnowledgeSourceModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _knowledge_source_from_row(row)

    async def get_sources(self, source_ids: list[str] | None = None) -> list[KnowledgeSource]:
        async with self.session_factory() as session:
            stmt = select(KnowledgeSourceModel)
            if source_ids:
                stmt = stmt.where(KnowledgeSourceModel.source_id.in_(source_ids))
            rows = (await session.execute(stmt)).scalars().all()
            sources = [_knowledge_source_from_row(row) for row in rows]
            if not source_ids:
                return sources
            by_id = {source.source_id: source for source in sources}
            return [by_id[sid] for sid in source_ids if sid in by_id]

    async def add_chunk(self, chunk: KnowledgeChunk) -> KnowledgeChunk:
        async with self.session_factory() as session:
            source = await session.get(KnowledgeSourceModel, chunk.source_id)
            if source is None:
                session.add(
                    KnowledgeSourceModel(
                        source_id=chunk.source_id,
                        name=chunk.source_id,
                    )
                )
            row = await session.get(KnowledgeChunkModel, chunk.chunk_id)
            values = _knowledge_chunk_values(chunk)
            if row is None:
                row = KnowledgeChunkModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _knowledge_chunk_from_row(row)

    async def search_chunks(
        self,
        *,
        query: str,
        source_ids: list[str],
        limit: int,
    ) -> list[tuple[KnowledgeChunk, float]]:
        async with self.session_factory() as session:
            stmt = select(KnowledgeChunkModel)
            if source_ids:
                stmt = stmt.where(KnowledgeChunkModel.source_id.in_(source_ids))
            rows = (await session.execute(stmt)).scalars().all()
        query_tokens = _tokens(query)
        scored: list[tuple[KnowledgeChunk, float]] = []
        for row in rows:
            chunk = _knowledge_chunk_from_row(row)
            content_tokens = _tokens(chunk.content)
            overlap = len(query_tokens & content_tokens)
            score = overlap / max(len(query_tokens), 1)
            if query_tokens and overlap == 0:
                continue
            scored.append((chunk, min(score or 0.1, 1.0)))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]

    async def add_log(self, log: KnowledgeRetrievalLog) -> KnowledgeRetrievalLog:
        async with self.session_factory() as session:
            row = KnowledgeRetrievalLogModel(**_knowledge_log_values(log))
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _knowledge_log_from_row(row)

    async def list_chunks(
        self,
        *,
        source_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[KnowledgeChunk]:
        async with self.session_factory() as session:
            stmt = select(KnowledgeChunkModel)
            if source_ids:
                stmt = stmt.where(KnowledgeChunkModel.source_id.in_(source_ids))
            stmt = stmt.order_by(desc(KnowledgeChunkModel.updated_at)).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_knowledge_chunk_from_row(row) for row in rows]

    async def list_logs(
        self,
        *,
        caller_type: str | None = None,
        caller_id: str | None = None,
        purpose: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeRetrievalLog]:
        async with self.session_factory() as session:
            stmt = select(KnowledgeRetrievalLogModel)
            if caller_type:
                stmt = stmt.where(KnowledgeRetrievalLogModel.caller_type == caller_type)
            if caller_id:
                stmt = stmt.where(KnowledgeRetrievalLogModel.caller_id == caller_id)
            if purpose:
                stmt = stmt.where(KnowledgeRetrievalLogModel.purpose == purpose)
            if tenant_id:
                stmt = stmt.where(
                    or_(
                        KnowledgeRetrievalLogModel.tenant_id.is_(None),
                        KnowledgeRetrievalLogModel.tenant_id == tenant_id,
                    )
                )
            stmt = stmt.order_by(desc(KnowledgeRetrievalLogModel.created_at)).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_knowledge_log_from_row(row) for row in rows]


def _memory_values(item: MemoryItem) -> dict:
    return {
        "memory_id": item.memory_id,
        "scope": str(item.scope),
        "subject_type": item.subject_type,
        "subject_id": item.subject_id,
        "user_id": item.user_id,
        "tenant_id": item.tenant_id,
        "agent_id": item.agent_id,
        "content": item.content,
        "structured_value_text": dumps(item.structured_value),
        "source": item.source,
        "confidence": int(item.confidence * 100),
        "importance": int(item.importance * 100),
        "visibility": item.visibility,
        "ttl_expires_at": item.ttl_expires_at,
        "metadata_text": dumps(item.metadata),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _memory_from_row(row: MemoryItemModel) -> MemoryItem:
    return MemoryItem(
        memory_id=row.memory_id,
        scope=row.scope,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        content=row.content,
        structured_value=loads(row.structured_value_text, {}),
        source=row.source,
        confidence=row.confidence / 100,
        importance=row.importance / 100,
        visibility=row.visibility,
        ttl_expires_at=row.ttl_expires_at,
        metadata=loads(row.metadata_text, {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _memory_event_values(event: MemoryEvent) -> dict:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "memory_id": event.memory_id,
        "user_id": event.user_id,
        "tenant_id": event.tenant_id,
        "agent_id": event.agent_id,
        "payload_text": dumps(event.payload),
        "created_at": event.created_at,
    }


def _memory_event_from_row(row: MemoryEventModel) -> MemoryEvent:
    return MemoryEvent(
        event_id=row.event_id,
        event_type=row.event_type,
        memory_id=row.memory_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        payload=loads(row.payload_text, {}),
        created_at=row.created_at,
    )


def _memory_active(item: MemoryItem) -> bool:
    if item.ttl_expires_at is None:
        return True
    expires_at = item.ttl_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)


def _knowledge_source_values(source: KnowledgeSource) -> dict:
    return {
        "source_id": source.source_id,
        "name": source.name,
        "description": source.description,
        "enabled": source.enabled,
        "allow_roles_text": dumps(source.allow_roles),
        "allow_groups_text": dumps(source.allow_groups),
        "allow_tenants_text": dumps(source.allow_tenants),
        "tags_text": dumps(source.tags),
        "metadata_text": dumps(source.metadata),
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


def _knowledge_source_from_row(row: KnowledgeSourceModel) -> KnowledgeSource:
    return KnowledgeSource(
        source_id=row.source_id,
        name=row.name,
        description=row.description,
        enabled=row.enabled,
        allow_roles=loads(row.allow_roles_text, []),
        allow_groups=loads(row.allow_groups_text, []),
        allow_tenants=loads(row.allow_tenants_text, []),
        tags=loads(row.tags_text, []),
        metadata=loads(row.metadata_text, {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _knowledge_chunk_values(chunk: KnowledgeChunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "source_id": chunk.source_id,
        "content": chunk.content,
        "title": chunk.title,
        "uri": chunk.uri,
        "tags_text": dumps(chunk.tags),
        "metadata_text": dumps(chunk.metadata),
        "updated_at": chunk.updated_at,
    }


def _knowledge_chunk_from_row(row: KnowledgeChunkModel) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=row.chunk_id,
        source_id=row.source_id,
        content=row.content,
        title=row.title,
        uri=row.uri,
        tags=loads(row.tags_text, []),
        metadata=loads(row.metadata_text, {}),
        updated_at=row.updated_at,
    )


def _knowledge_log_values(log: KnowledgeRetrievalLog) -> dict:
    return {
        "log_id": log.log_id,
        "query": log.query,
        "caller_type": log.caller_type,
        "caller_id": log.caller_id,
        "purpose": log.purpose,
        "user_id": log.user_id,
        "tenant_id": log.tenant_id,
        "selected_source_ids_text": dumps(log.selected_source_ids),
        "denied_source_ids_text": dumps(log.denied_source_ids),
        "hit_count": log.hit_count,
        "status": log.status,
        "errors_text": dumps(log.errors),
        "metadata_text": dumps(log.metadata),
        "created_at": log.created_at,
    }


def _knowledge_log_from_row(row: KnowledgeRetrievalLogModel) -> KnowledgeRetrievalLog:
    return KnowledgeRetrievalLog(
        log_id=row.log_id,
        query=row.query,
        caller_type=row.caller_type,
        caller_id=row.caller_id,
        purpose=row.purpose,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        selected_source_ids=loads(row.selected_source_ids_text, []),
        denied_source_ids=loads(row.denied_source_ids_text, []),
        hit_count=row.hit_count,
        status=row.status,
        errors=loads(row.errors_text, []),
        metadata=loads(row.metadata_text, {}),
        created_at=row.created_at,
    )
