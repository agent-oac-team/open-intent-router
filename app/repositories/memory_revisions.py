import asyncio
from copy import deepcopy

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import MemoryItemModel, MemoryRevisionModel
from app.repositories.context_stores import _memory_from_row
from app.repositories.json_utils import dumps, loads
from app.schemas.memory import MemoryIndexStatus, MemoryItem, MemoryRevision


class MemoryRevisionLedgerRepository:
    def __init__(self, item_repository) -> None:
        self.item_repository = item_repository
        self.revisions: dict[str, list[MemoryRevision]] = {}
        self._lock = asyncio.Lock()

    async def list_for_memory(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        user_id: str,
        subject_type: str,
        subject_id: str,
        limit: int = 100,
    ) -> list[MemoryRevision]:
        item = self.item_repository.items.get(memory_id)
        if not _matches_owner(
            item,
            tenant_id=tenant_id,
            user_id=user_id,
            subject_type=subject_type,
            subject_id=subject_id,
        ):
            return []
        return [
            revision.model_copy(deep=True)
            for revision in self.revisions.get(memory_id, [])[-max(1, min(limit, 100)) :]
        ]

    async def append_revision_and_set_current(
        self,
        revision: MemoryRevision,
        *,
        tenant_id: str,
        user_id: str,
        subject_type: str,
        subject_id: str,
        expected_current_revision_id: str | None,
    ) -> tuple[MemoryRevision, MemoryItem]:
        async with self._lock:
            item = self.item_repository.items.get(revision.memory_id)
            _validate_revision_target(
                item,
                revision,
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type=subject_type,
                subject_id=subject_id,
                expected_current_revision_id=expected_current_revision_id,
            )
            existing = self.revisions.setdefault(revision.memory_id, [])
            stored = revision.model_copy(
                deep=True,
                update={
                    "revision_no": len(existing) + 1,
                    "supersedes_revision_id": item.current_revision_id,
                },
            )
            if any(value.revision_id == stored.revision_id for value in existing):
                raise ValueError("Memory revision already exists")
            updated = item.model_copy(
                deep=True,
                update={
                    "content": stored.content,
                    "structured_value": deepcopy(stored.structured_value),
                    "current_revision_id": stored.revision_id,
                    "current_revision_no": stored.revision_no,
                    "formation_job_id": stored.formation_job_id,
                    "index_status": MemoryIndexStatus.PENDING,
                    "updated_at": stored.created_at,
                },
            )
            existing.append(stored.model_copy(deep=True))
            self.item_repository.items[item.memory_id] = updated
            return stored.model_copy(deep=True), updated.model_copy(deep=True)


class DatabaseMemoryRevisionLedgerRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def list_for_memory(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        user_id: str,
        subject_type: str,
        subject_id: str,
        limit: int = 100,
    ) -> list[MemoryRevision]:
        async with self.session_factory() as session:
            owned = await session.scalar(
                select(MemoryItemModel.memory_id).where(
                    MemoryItemModel.memory_id == memory_id,
                    MemoryItemModel.tenant_id == tenant_id,
                    MemoryItemModel.user_id == user_id,
                    MemoryItemModel.subject_type == subject_type,
                    MemoryItemModel.subject_id == subject_id,
                )
            )
            if owned is None:
                return []
            rows = (
                (
                    await session.execute(
                        select(MemoryRevisionModel)
                        .where(MemoryRevisionModel.memory_id == memory_id)
                        .order_by(MemoryRevisionModel.revision_no.desc())
                        .limit(max(1, min(limit, 100)))
                    )
                )
                .scalars()
                .all()
            )
            return [_revision_from_row(row) for row in reversed(rows)]

    async def append_revision_and_set_current(
        self,
        revision: MemoryRevision,
        *,
        tenant_id: str,
        user_id: str,
        subject_type: str,
        subject_id: str,
        expected_current_revision_id: str | None,
    ) -> tuple[MemoryRevision, MemoryItem]:
        async with self.session_factory() as session:
            item = await session.scalar(
                select(MemoryItemModel)
                .where(
                    MemoryItemModel.memory_id == revision.memory_id,
                    MemoryItemModel.tenant_id == tenant_id,
                    MemoryItemModel.user_id == user_id,
                    MemoryItemModel.subject_type == subject_type,
                    MemoryItemModel.subject_id == subject_id,
                )
                .with_for_update()
            )
            _validate_revision_target(
                _memory_from_row(item) if item is not None else None,
                revision,
                tenant_id=tenant_id,
                user_id=user_id,
                subject_type=subject_type,
                subject_id=subject_id,
                expected_current_revision_id=expected_current_revision_id,
            )
            latest_no = await session.scalar(
                select(func.max(MemoryRevisionModel.revision_no)).where(
                    MemoryRevisionModel.memory_id == revision.memory_id
                )
            )
            stored = revision.model_copy(
                update={
                    "revision_no": int(latest_no or 0) + 1,
                    "supersedes_revision_id": item.current_revision_id,
                }
            )
            current_condition = (
                MemoryItemModel.current_revision_id.is_(None)
                if expected_current_revision_id is None
                else MemoryItemModel.current_revision_id == expected_current_revision_id
            )
            result = await session.execute(
                update(MemoryItemModel)
                .where(
                    MemoryItemModel.memory_id == stored.memory_id,
                    MemoryItemModel.tenant_id == tenant_id,
                    MemoryItemModel.user_id == user_id,
                    MemoryItemModel.subject_type == subject_type,
                    MemoryItemModel.subject_id == subject_id,
                    MemoryItemModel.lifecycle_status == "active",
                    current_condition,
                )
                .values(
                    content=stored.content,
                    structured_value_text=dumps(stored.structured_value),
                    current_revision_id=stored.revision_id,
                    formation_job_id=stored.formation_job_id,
                    index_status="pending",
                    updated_at=stored.created_at,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise ValueError("Memory current revision changed")
            session.add(MemoryRevisionModel(**_revision_values(stored)))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("Memory revision sequence conflict") from exc
            refreshed = await session.get(MemoryItemModel, stored.memory_id)
            current = _memory_from_row(refreshed).model_copy(
                update={"current_revision_no": stored.revision_no}
            )
            return stored, current


def _validate_revision_target(
    item: MemoryItem | None,
    revision: MemoryRevision,
    *,
    tenant_id: str,
    user_id: str,
    subject_type: str,
    subject_id: str,
    expected_current_revision_id: str | None,
) -> None:
    if not _matches_owner(
        item,
        tenant_id=tenant_id,
        user_id=user_id,
        subject_type=subject_type,
        subject_id=subject_id,
    ):
        raise ValueError("Memory revision target not found")
    if item.lifecycle_status != "active":
        raise ValueError("Memory revision target is not active")
    if item.memory_key != revision.memory_key:
        raise ValueError("Memory revision key mismatch")
    if item.current_revision_id != expected_current_revision_id:
        raise ValueError("Memory current revision changed")


def _matches_owner(
    item: MemoryItem | None,
    *,
    tenant_id: str,
    user_id: str,
    subject_type: str,
    subject_id: str,
) -> bool:
    return bool(
        item is not None
        and item.tenant_id == tenant_id
        and item.user_id == user_id
        and item.subject_type == subject_type
        and item.subject_id == subject_id
    )


def _revision_values(revision: MemoryRevision) -> dict:
    return {
        "revision_id": revision.revision_id,
        "memory_id": revision.memory_id,
        "revision_no": revision.revision_no,
        "memory_key": revision.memory_key,
        "operation": revision.operation.value,
        "content": revision.content,
        "structured_value_text": dumps(revision.structured_value),
        "evidence_refs_text": dumps(
            [reference.model_dump(mode="json") for reference in revision.evidence_refs]
        ),
        "confidence": int(revision.confidence * 100),
        "policy_version": revision.policy_version,
        "supersedes_revision_id": revision.supersedes_revision_id,
        "formation_job_id": revision.formation_job_id,
        "created_at": revision.created_at,
    }


def _revision_from_row(row) -> MemoryRevision:
    return MemoryRevision(
        revision_id=row.revision_id,
        memory_id=row.memory_id,
        revision_no=row.revision_no,
        memory_key=row.memory_key,
        operation=row.operation,
        content=row.content,
        structured_value=loads(row.structured_value_text, {}),
        evidence_refs=loads(row.evidence_refs_text, []),
        confidence=row.confidence / 100,
        policy_version=row.policy_version,
        supersedes_revision_id=row.supersedes_revision_id,
        formation_job_id=row.formation_job_id,
        created_at=row.created_at,
    )
