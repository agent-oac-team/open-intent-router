import asyncio
import hashlib
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.redaction import is_sensitive_key
from app.db.models import (
    MemoryEventModel,
    MemoryFormationJobModel,
    MemoryFormationTurnModel,
    MemoryIndexOperationModel,
    MemoryItemModel,
    MemoryRevisionModel,
)
from app.repositories.context_stores import (
    _memory_event_from_row,
    _memory_event_values,
    _memory_from_row,
    _memory_values,
)
from app.repositories.json_utils import dumps, loads
from app.repositories.memory_index_operations import _operation_from_row, _operation_values
from app.repositories.memory_revisions import _revision_from_row, _revision_values
from app.schemas.memory import (
    MemoryEvent,
    MemoryIndexOperation,
    MemoryIndexStatus,
    MemoryItem,
    MemoryLifecycleOperation,
    MemoryLifecycleStatus,
    MemoryRevision,
)


class _ReplayRequired(Exception):
    pass


class MemoryLifecycleStore:
    def __init__(
        self,
        *,
        item_repository,
        revision_repository,
        index_repository,
        formation_repository=None,
    ) -> None:
        self.item_repository = item_repository
        self.revision_repository = revision_repository
        self.index_repository = index_repository
        self.formation_repository = formation_repository
        self._lock = asyncio.Lock()

    async def commit_add(
        self,
        *,
        item: MemoryItem,
        revision: MemoryRevision,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
    ) -> tuple[MemoryItem, MemoryRevision, MemoryEvent, MemoryIndexOperation, bool]:
        async with self._lock:
            replay = self._load_replay(
                expected_event=event,
                expected_revision=revision,
                expected_index=index_operation,
                expected_item=item,
            )
            if replay is not None:
                return (*replay, True)
            if item.memory_id in self.item_repository.items:
                raise ValueError("Memory lifecycle ADD identity already exists")
            _validate_active_key_available(self.item_repository.items.values(), item)
            _validate_index_identity(self.index_repository, index_operation)
            stored_revision = revision.model_copy(
                deep=True, update={"revision_no": 1, "supersedes_revision_id": None}
            )
            stored_item = item.model_copy(
                deep=True,
                update={
                    "current_revision_id": stored_revision.revision_id,
                    "current_revision_no": 1,
                    "index_status": MemoryIndexStatus.PENDING,
                },
            )
            stored_event = event.model_copy(deep=True)
            stored_index = index_operation.model_copy(deep=True)
            self.item_repository.items[stored_item.memory_id] = stored_item
            self.revision_repository.revisions[stored_item.memory_id] = [stored_revision]
            self.item_repository.events.append(stored_event)
            self.index_repository.operations[stored_index.index_operation_id] = stored_index
            self.index_repository.by_idempotency[stored_index.idempotency_key] = (
                stored_index.index_operation_id
            )
            return _copy_commit(stored_item, stored_revision, stored_event, stored_index, False)

    async def commit_update(
        self,
        *,
        operation: MemoryLifecycleOperation,
        revision: MemoryRevision,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
        candidate_hash: str,
        confidence: float,
        importance: float,
        canonical_refs: list[str],
    ) -> tuple[MemoryItem, MemoryRevision, MemoryEvent, MemoryIndexOperation, bool]:
        async with self._lock:
            memory_id = _required_memory_id(operation)
            replay = self._load_replay(
                expected_event=event,
                expected_revision=revision,
                expected_index=index_operation,
                expected_operation=operation,
            )
            if replay is not None:
                return (*replay, True)
            item = self.item_repository.items.get(memory_id)
            _validate_operation_target(item, operation, require_active=True)
            if item.current_revision_id != operation.revision_id:
                raise ValueError("Memory lifecycle precondition changed")
            _validate_index_identity(self.index_repository, index_operation)
            existing = self.revision_repository.revisions.setdefault(memory_id, [])
            stored_revision = revision.model_copy(
                deep=True,
                update={
                    "revision_no": len(existing) + 1,
                    "supersedes_revision_id": item.current_revision_id,
                },
            )
            updated = item.model_copy(
                deep=True,
                update={
                    "content": stored_revision.content,
                    "structured_value": deepcopy(stored_revision.structured_value),
                    "candidate_hash": candidate_hash,
                    "confidence": confidence,
                    "importance": importance,
                    "current_revision_id": stored_revision.revision_id,
                    "current_revision_no": stored_revision.revision_no,
                    "formation_job_id": operation.formation_job_id,
                    "index_status": MemoryIndexStatus.PENDING,
                    "canonical_refs": list(canonical_refs),
                    "updated_at": stored_revision.created_at,
                },
            )
            stored_event = event.model_copy(deep=True)
            stored_index = index_operation.model_copy(deep=True)
            existing.append(stored_revision)
            self.item_repository.items[memory_id] = updated
            self.item_repository.events.append(stored_event)
            self.index_repository.operations[stored_index.index_operation_id] = stored_index
            self.index_repository.by_idempotency[stored_index.idempotency_key] = (
                stored_index.index_operation_id
            )
            return _copy_commit(updated, stored_revision, stored_event, stored_index, False)

    async def record_decision(self, event: MemoryEvent) -> tuple[MemoryEvent, bool]:
        async with self._lock:
            existing = next(
                (
                    value
                    for value in self.item_repository.events
                    if value.event_id == event.event_id
                ),
                None,
            )
            if existing is not None:
                _validate_event_replay(existing, event)
                return existing.model_copy(deep=True), True
            stored = event.model_copy(deep=True)
            self.item_repository.events.append(stored)
            return stored.model_copy(deep=True), False

    async def request_delete(
        self,
        *,
        operation: MemoryLifecycleOperation,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
    ) -> tuple[MemoryItem | None, MemoryEvent, MemoryIndexOperation, bool]:
        async with self._lock:
            memory_id = _required_memory_id(operation)
            existing_event = next(
                (
                    value
                    for value in self.item_repository.events
                    if value.event_id == event.event_id
                ),
                None,
            )
            if existing_event is not None:
                item = self.item_repository.items.get(memory_id)
                existing_index = self.index_repository.operations.get(
                    self.index_repository.by_idempotency.get(index_operation.idempotency_key, "")
                )
                if existing_index is None:
                    raise ValueError("Incomplete deletion replay state")
                _validate_event_replay(existing_event, event)
                _validate_index_replay(existing_index, index_operation)
                if item is not None:
                    _validate_operation_target(item, operation, require_active=False)
                    if item.current_revision_id != operation.revision_id:
                        raise ValueError("Memory lifecycle replay identity conflict")
                elif existing_index.status != "completed":
                    raise ValueError("Incomplete deletion replay state")
                return (
                    item.model_copy(deep=True) if item is not None else None,
                    existing_event.model_copy(deep=True),
                    existing_index.model_copy(deep=True),
                    True,
                )
            item = self.item_repository.items.get(memory_id)
            _validate_operation_target(item, operation, require_active=True)
            if item.current_revision_id != operation.revision_id:
                raise ValueError("Memory lifecycle precondition changed")
            _validate_index_identity(self.index_repository, index_operation)
            external_id = item.metadata.get("mem0_memory_id")
            stored_index = index_operation.model_copy(
                deep=True,
                update={
                    "external_memory_id": external_id if isinstance(external_id, str) else None
                },
            )
            deleting = item.model_copy(
                deep=True,
                update={
                    "lifecycle_status": MemoryLifecycleStatus.DELETION_PENDING,
                    "index_status": MemoryIndexStatus.DELETION_PENDING,
                    "updated_at": event.created_at,
                },
            )
            stored_event = event.model_copy(deep=True)
            self.item_repository.items[memory_id] = deleting
            self.item_repository.events.append(stored_event)
            self.index_repository.operations[stored_index.index_operation_id] = stored_index
            self.index_repository.by_idempotency[stored_index.idempotency_key] = (
                stored_index.index_operation_id
            )
            await self._scrub_pending_payloads(operation)
            self._scrub_memory_events(operation)
            return (
                deleting.model_copy(deep=True),
                stored_event.model_copy(deep=True),
                stored_index.model_copy(deep=True),
                False,
            )

    async def complete_delete(
        self,
        *,
        operation: MemoryLifecycleOperation,
        tombstone: MemoryEvent,
        index_operation_id: str,
    ) -> tuple[MemoryEvent, bool]:
        async with self._lock:
            existing = next(
                (
                    value
                    for value in self.item_repository.events
                    if value.event_id == tombstone.event_id
                ),
                None,
            )
            if existing is not None:
                _validate_event_replay(existing, tombstone)
                return existing.model_copy(deep=True), True
            memory_id = _required_memory_id(operation)
            _validate_completed_delete_index(
                self.index_repository.operations.get(index_operation_id), operation
            )
            item = self.item_repository.items.get(memory_id)
            _validate_operation_target(item, operation, require_active=False)
            if item.lifecycle_status != MemoryLifecycleStatus.DELETION_PENDING:
                raise ValueError("Memory deletion is not pending")
            self.revision_repository.revisions.pop(memory_id, None)
            self.item_repository.items.pop(memory_id, None)
            stored = tombstone.model_copy(deep=True)
            self.item_repository.events.append(stored)
            return stored.model_copy(deep=True), False

    async def record_delete_failure(
        self,
        *,
        operation: MemoryLifecycleOperation,
        event: MemoryEvent,
        dead_letter: bool,
    ) -> tuple[MemoryItem, MemoryEvent, bool]:
        async with self._lock:
            existing = next(
                (
                    value
                    for value in self.item_repository.events
                    if value.event_id == event.event_id
                ),
                None,
            )
            memory_id = _required_memory_id(operation)
            item = self.item_repository.items.get(memory_id)
            _validate_operation_target(item, operation, require_active=False)
            if item.lifecycle_status != MemoryLifecycleStatus.DELETION_PENDING:
                raise ValueError("Memory deletion is not pending")
            if existing is not None:
                return item.model_copy(deep=True), existing.model_copy(deep=True), True
            updates = {
                "lifecycle_status": MemoryLifecycleStatus.DELETION_PENDING,
                "index_status": (
                    MemoryIndexStatus.DEAD_LETTER
                    if dead_letter
                    else MemoryIndexStatus.DELETION_PENDING
                ),
                "updated_at": event.created_at,
            }
            if dead_letter:
                updates.update(_scrubbed_item_values(item))
                self.revision_repository.revisions[memory_id] = [
                    _scrub_revision(value)
                    for value in self.revision_repository.revisions.get(memory_id, [])
                ]
            failed = item.model_copy(deep=True, update=updates)
            stored = event.model_copy(deep=True)
            self.item_repository.items[memory_id] = failed
            self.item_repository.events.append(stored)
            return failed.model_copy(deep=True), stored.model_copy(deep=True), False

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[MemoryItem]:
        async with self._lock:
            values = [
                item
                for item in self.item_repository.items.values()
                if item.lifecycle_status == MemoryLifecycleStatus.ACTIVE
                and item.ttl_expires_at is not None
                and _as_utc(item.ttl_expires_at) <= _as_utc(now)
            ]
            return [
                item.model_copy(deep=True)
                for item in sorted(
                    values, key=lambda value: (_as_utc(value.ttl_expires_at), value.memory_id)
                )[:limit]
            ]

    async def get_item(self, memory_id: str) -> MemoryItem | None:
        item = self.item_repository.items.get(memory_id)
        return item.model_copy(deep=True) if item is not None else None

    async def complete_delete_from_index(
        self,
        index_operation: MemoryIndexOperation,
        *,
        now: datetime,
        provider_status: str | None = None,
    ) -> tuple[MemoryEvent, bool]:
        async with self._lock:
            stored_index = self.index_repository.operations.get(index_operation.index_operation_id)
            _validate_delete_index_completion(stored_index, index_operation)
            event_id = _index_event_id(index_operation, "delete-completed")
            existing = next(
                (event for event in self.item_repository.events if event.event_id == event_id),
                None,
            )
            if existing is not None:
                return existing.model_copy(deep=True), True
            item = self.item_repository.items.get(index_operation.memory_id)
            _validate_delete_index_item(item, index_operation)
            request = _delete_request_event(self.item_repository.events, index_operation.memory_id)
            tombstone = _delete_index_event(
                index_operation,
                item=item,
                request=request,
                event_id=event_id,
                event_type="memory_deleted_tombstone",
                provider_status=_delete_provider_status(index_operation, provider_status),
                now=now,
            )
            self.revision_repository.revisions.pop(index_operation.memory_id, None)
            self.item_repository.items.pop(index_operation.memory_id, None)
            self.item_repository.events.append(tombstone)
            return tombstone.model_copy(deep=True), False

    async def record_delete_index_failure(
        self,
        index_operation: MemoryIndexOperation,
        *,
        error_code: str,
        dead_letter: bool,
        now: datetime,
    ) -> tuple[MemoryItem, MemoryEvent, bool]:
        async with self._lock:
            item = self.item_repository.items.get(index_operation.memory_id)
            _validate_delete_index_item(item, index_operation)
            suffix = f"delete-{'dead-letter' if dead_letter else 'retry'}:{index_operation.attempt_count}"
            event_id = _index_event_id(index_operation, suffix)
            existing = next(
                (event for event in self.item_repository.events if event.event_id == event_id),
                None,
            )
            if existing is not None:
                return item.model_copy(deep=True), existing.model_copy(deep=True), True
            request = _delete_request_event(self.item_repository.events, index_operation.memory_id)
            event = _delete_index_event(
                index_operation,
                item=item,
                request=request,
                event_id=event_id,
                event_type=(
                    "memory_deletion_dead_letter" if dead_letter else "memory_deletion_retry"
                ),
                provider_status="dead_letter" if dead_letter else "retry",
                now=now,
                error_code=error_code,
            )
            updates = {
                "index_status": (
                    MemoryIndexStatus.DEAD_LETTER
                    if dead_letter
                    else MemoryIndexStatus.DELETION_PENDING
                ),
                "updated_at": now,
            }
            if dead_letter:
                updates.update(_scrubbed_item_values(item))
                self.revision_repository.revisions[index_operation.memory_id] = [
                    _scrub_revision(revision)
                    for revision in self.revision_repository.revisions.get(
                        index_operation.memory_id, []
                    )
                ]
            stored = item.model_copy(deep=True, update=updates)
            self.item_repository.items[index_operation.memory_id] = stored
            self.item_repository.events.append(event)
            return stored.model_copy(deep=True), event.model_copy(deep=True), False

    def _load_replay(
        self,
        *,
        expected_event: MemoryEvent,
        expected_revision: MemoryRevision,
        expected_index: MemoryIndexOperation,
        expected_item: MemoryItem | None = None,
        expected_operation: MemoryLifecycleOperation | None = None,
    ):
        event = next(
            (
                value
                for value in self.item_repository.events
                if value.event_id == expected_event.event_id
            ),
            None,
        )
        if event is None:
            return None
        memory_id = expected_revision.memory_id
        item = self.item_repository.items.get(memory_id)
        revision = next(
            (
                value
                for value in self.revision_repository.revisions.get(memory_id, [])
                if value.revision_id == expected_revision.revision_id
            ),
            None,
        )
        index = next(
            (
                value
                for value in self.index_repository.operations.values()
                if value.memory_id == memory_id
                and value.revision_id == expected_revision.revision_id
            ),
            None,
        )
        if item is None or revision is None or index is None:
            raise ValueError("Incomplete lifecycle replay state")
        _validate_event_replay(event, expected_event)
        _validate_revision_replay(revision, expected_revision)
        _validate_index_replay(index, expected_index)
        if expected_item is not None:
            _validate_item_replay(item, expected_item)
        elif expected_operation is not None:
            _validate_operation_target(item, expected_operation, require_active=False)
        return _copy_commit(item, revision, event, index, False)[:4]

    async def _scrub_pending_payloads(self, operation: MemoryLifecycleOperation) -> None:
        repository = self.formation_repository
        if repository is None:
            return
        async with repository._lock:
            self._scrub_pending_payloads_locked(repository, operation)

    def _scrub_pending_payloads_locked(
        self, repository, operation: MemoryLifecycleOperation
    ) -> None:
        memory_id = _required_memory_id(operation)
        matched_turns: set[str] = set()
        for turn_id, turn in list(repository.turns.items()):
            if turn.tenant_id != operation.tenant_id or turn.user_id != operation.user_id:
                continue
            if turn.status not in {"pending", "claimed"}:
                continue
            if turn_id in operation.canonical_refs or memory_id in turn.used_memory_ids:
                matched_turns.add(turn_id)
                repository.turns[turn_id] = turn.model_copy(
                    deep=True,
                    update={
                        "user_text": "",
                        "assistant_text": "",
                        "used_memory_ids": [
                            value for value in turn.used_memory_ids if value != memory_id
                        ],
                    },
                )
        for job_id, job in list(repository.jobs.items()):
            if job.tenant_id != operation.tenant_id or job.user_id != operation.user_id:
                continue
            if job.status not in {"pending", "claimed", "retry"}:
                continue
            if matched_turns.intersection(job.source_refs) or _payload_contains(
                job.trace_summary, memory_id
            ):
                repository.jobs[job_id] = job.model_copy(
                    deep=True, update={"trace_summary": _scrub_payload(job.trace_summary)}
                )

    def _scrub_memory_events(self, operation: MemoryLifecycleOperation) -> None:
        memory_id = _required_memory_id(operation)
        for index, event in enumerate(self.item_repository.events):
            if (
                event.memory_id == memory_id
                and event.tenant_id == operation.tenant_id
                and event.user_id == operation.user_id
            ):
                self.item_repository.events[index] = event.model_copy(
                    deep=True,
                    update={"payload": _scrub_payload(event.payload)},
                )


class DatabaseMemoryLifecycleStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def commit_add(
        self,
        *,
        item: MemoryItem,
        revision: MemoryRevision,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
    ) -> tuple[MemoryItem, MemoryRevision, MemoryEvent, MemoryIndexOperation, bool]:
        replay = await self._load_replay(
            expected_event=event,
            expected_revision=revision,
            expected_index=index_operation,
            expected_item=item,
        )
        if replay is not None:
            return (*replay, True)
        stored_revision = revision.model_copy(
            update={"revision_no": 1, "supersedes_revision_id": None}
        )
        stored_item = item.model_copy(
            update={
                "current_revision_id": stored_revision.revision_id,
                "current_revision_no": 1,
                "index_status": MemoryIndexStatus.PENDING,
            }
        )
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    session.add(MemoryItemModel(**_memory_values(stored_item)))
                    session.add(MemoryRevisionModel(**_revision_values(stored_revision)))
                    session.add(MemoryEventModel(**_memory_event_values(event)))
                    session.add(MemoryIndexOperationModel(**_operation_values(index_operation)))
            except IntegrityError as exc:
                await session.rollback()
                replay = await self._load_replay(
                    expected_event=event,
                    expected_revision=revision,
                    expected_index=index_operation,
                    expected_item=item,
                )
                if replay is None:
                    raise ValueError("Memory lifecycle ADD conflict") from exc
                return (*replay, True)
        return _copy_commit(stored_item, stored_revision, event, index_operation, False)

    async def commit_update(
        self,
        *,
        operation: MemoryLifecycleOperation,
        revision: MemoryRevision,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
        candidate_hash: str,
        confidence: float,
        importance: float,
        canonical_refs: list[str],
    ) -> tuple[MemoryItem, MemoryRevision, MemoryEvent, MemoryIndexOperation, bool]:
        memory_id = _required_memory_id(operation)
        replay = await self._load_replay(
            expected_event=event,
            expected_revision=revision,
            expected_index=index_operation,
            expected_operation=operation,
        )
        if replay is not None:
            return (*replay, True)
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    row = await session.scalar(
                        select(MemoryItemModel)
                        .where(MemoryItemModel.memory_id == memory_id)
                        .with_for_update()
                    )
                    item = _memory_from_row(row) if row is not None else None
                    _validate_operation_target(item, operation, require_active=True)
                    if item.current_revision_id != operation.revision_id:
                        raise ValueError("Memory lifecycle precondition changed")
                    latest_no = await session.scalar(
                        select(func.max(MemoryRevisionModel.revision_no)).where(
                            MemoryRevisionModel.memory_id == memory_id
                        )
                    )
                    stored_revision = revision.model_copy(
                        update={
                            "revision_no": int(latest_no or 0) + 1,
                            "supersedes_revision_id": item.current_revision_id,
                        }
                    )
                    result = await session.execute(
                        update(MemoryItemModel)
                        .where(
                            MemoryItemModel.memory_id == memory_id,
                            MemoryItemModel.tenant_id == operation.tenant_id,
                            MemoryItemModel.user_id == operation.user_id,
                            MemoryItemModel.subject_type == operation.subject_type,
                            MemoryItemModel.subject_id == operation.subject_id,
                            _database_memory_key_condition(operation),
                            MemoryItemModel.lifecycle_status == "active",
                            MemoryItemModel.current_revision_id == operation.revision_id,
                        )
                        .values(
                            content=stored_revision.content,
                            structured_value_text=dumps(stored_revision.structured_value),
                            candidate_hash=candidate_hash,
                            confidence=int(confidence * 100),
                            importance=int(importance * 100),
                            current_revision_id=stored_revision.revision_id,
                            formation_job_id=operation.formation_job_id,
                            index_status="pending",
                            canonical_refs_text=dumps(canonical_refs),
                            updated_at=stored_revision.created_at,
                        )
                    )
                    if result.rowcount != 1:
                        raise ValueError("Memory lifecycle precondition changed")
                    session.add(MemoryRevisionModel(**_revision_values(stored_revision)))
                    session.add(MemoryEventModel(**_memory_event_values(event)))
                    session.add(MemoryIndexOperationModel(**_operation_values(index_operation)))
            except IntegrityError as exc:
                await session.rollback()
                replay = await self._load_replay(
                    expected_event=event,
                    expected_revision=revision,
                    expected_index=index_operation,
                    expected_operation=operation,
                )
                if replay is None:
                    raise ValueError("Memory lifecycle UPDATE conflict") from exc
                return (*replay, True)
        current = item.model_copy(
            update={
                "content": stored_revision.content,
                "structured_value": deepcopy(stored_revision.structured_value),
                "candidate_hash": candidate_hash,
                "confidence": confidence,
                "importance": importance,
                "current_revision_id": stored_revision.revision_id,
                "current_revision_no": stored_revision.revision_no,
                "formation_job_id": operation.formation_job_id,
                "index_status": MemoryIndexStatus.PENDING,
                "canonical_refs": list(canonical_refs),
                "updated_at": stored_revision.created_at,
            }
        )
        return _copy_commit(current, stored_revision, event, index_operation, False)

    async def record_decision(self, event: MemoryEvent) -> tuple[MemoryEvent, bool]:
        async with self.session_factory() as session:
            existing = await session.get(MemoryEventModel, event.event_id)
            if existing is not None:
                stored = _memory_event_from_row(existing)
                _validate_event_replay(stored, event)
                return stored, True
            session.add(MemoryEventModel(**_memory_event_values(event)))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.get(MemoryEventModel, event.event_id)
                stored = _memory_event_from_row(existing)
                _validate_event_replay(stored, event)
                return stored, True
            return event, False

    async def request_delete(
        self,
        *,
        operation: MemoryLifecycleOperation,
        event: MemoryEvent,
        index_operation: MemoryIndexOperation,
    ) -> tuple[MemoryItem | None, MemoryEvent, MemoryIndexOperation, bool]:
        memory_id = _required_memory_id(operation)
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    result = await session.execute(
                        update(MemoryItemModel)
                        .where(
                            MemoryItemModel.memory_id == memory_id,
                            MemoryItemModel.tenant_id == operation.tenant_id,
                            MemoryItemModel.user_id == operation.user_id,
                            MemoryItemModel.subject_type == operation.subject_type,
                            MemoryItemModel.subject_id == operation.subject_id,
                            _database_memory_key_condition(operation),
                            MemoryItemModel.lifecycle_status == "active",
                            (
                                MemoryItemModel.current_revision_id.is_(None)
                                if operation.revision_id is None
                                else MemoryItemModel.current_revision_id == operation.revision_id
                            ),
                        )
                        .values(
                            lifecycle_status="deletion_pending",
                            index_status="deletion_pending",
                            updated_at=event.created_at,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount != 1:
                        raise _ReplayRequired
                    row = await session.get(MemoryItemModel, memory_id)
                    item = _memory_from_row(row)
                    external_id = item.metadata.get("mem0_memory_id")
                    stored_index = index_operation.model_copy(
                        update={
                            "external_memory_id": (
                                external_id if isinstance(external_id, str) else None
                            )
                        }
                    )
                    session.add(MemoryEventModel(**_memory_event_values(event)))
                    session.add(MemoryIndexOperationModel(**_operation_values(stored_index)))
                    await self._scrub_pending_payloads(session, operation)
            except (_ReplayRequired, IntegrityError, OperationalError):
                await session.rollback()
                replay = await self._wait_for_delete_replay(
                    operation=operation,
                    expected_event=event,
                    expected_index=index_operation,
                )
                if replay is None:
                    raise ValueError("Memory lifecycle target is not active") from None
                return (*replay, True)
        deleting = item.model_copy(
            update={
                "lifecycle_status": MemoryLifecycleStatus.DELETION_PENDING,
                "index_status": MemoryIndexStatus.DELETION_PENDING,
                "updated_at": event.created_at,
            }
        )
        return deleting, event, stored_index, False

    async def complete_delete(
        self,
        *,
        operation: MemoryLifecycleOperation,
        tombstone: MemoryEvent,
        index_operation_id: str,
    ) -> tuple[MemoryEvent, bool]:
        memory_id = _required_memory_id(operation)
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    index_row = await session.get(MemoryIndexOperationModel, index_operation_id)
                    _validate_completed_delete_index(
                        _operation_from_row(index_row) if index_row is not None else None,
                        operation,
                    )
                    result = await session.execute(
                        delete(MemoryItemModel).where(
                            MemoryItemModel.memory_id == memory_id,
                            MemoryItemModel.tenant_id == operation.tenant_id,
                            MemoryItemModel.user_id == operation.user_id,
                            MemoryItemModel.subject_type == operation.subject_type,
                            MemoryItemModel.subject_id == operation.subject_id,
                            _database_memory_key_condition(operation),
                            MemoryItemModel.lifecycle_status == "deletion_pending",
                            (
                                MemoryItemModel.current_revision_id.is_(None)
                                if operation.revision_id is None
                                else MemoryItemModel.current_revision_id == operation.revision_id
                            ),
                        )
                    )
                    if result.rowcount != 1:
                        raise _ReplayRequired
                    await session.execute(
                        delete(MemoryRevisionModel).where(
                            MemoryRevisionModel.memory_id == memory_id
                        )
                    )
                    session.add(MemoryEventModel(**_memory_event_values(tombstone)))
            except (_ReplayRequired, IntegrityError, OperationalError):
                await session.rollback()
                existing = await self._wait_for_event_replay(tombstone)
                if existing is None:
                    raise ValueError("Memory deletion is not pending") from None
                return existing, True
        return tombstone, False

    async def record_delete_failure(
        self,
        *,
        operation: MemoryLifecycleOperation,
        event: MemoryEvent,
        dead_letter: bool,
    ) -> tuple[MemoryItem, MemoryEvent, bool]:
        memory_id = _required_memory_id(operation)
        async with self.session_factory() as session:
            existing = await session.get(MemoryEventModel, event.event_id)
            row = await session.scalar(
                select(MemoryItemModel)
                .where(MemoryItemModel.memory_id == memory_id)
                .with_for_update()
            )
            item = _memory_from_row(row) if row is not None else None
            _validate_operation_target(item, operation, require_active=False)
            if item.lifecycle_status != MemoryLifecycleStatus.DELETION_PENDING:
                raise ValueError("Memory deletion is not pending")
            if existing is not None:
                return item, _memory_event_from_row(existing), True
            async with session.begin_nested():
                row.index_status = "dead_letter" if dead_letter else "deletion_pending"
                row.updated_at = event.created_at
                if dead_letter:
                    values = _scrubbed_item_values(item)
                    row.content = values["content"]
                    row.structured_value_text = dumps(values["structured_value"])
                    row.metadata_text = dumps(values["metadata"])
                    row.candidate_hash = values["candidate_hash"]
                    row.canonical_refs_text = dumps(values["canonical_refs"])
                    row.formation_job_id = values["formation_job_id"]
                    revisions = (
                        (
                            await session.execute(
                                select(MemoryRevisionModel).where(
                                    MemoryRevisionModel.memory_id == memory_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for revision in revisions:
                        revision.content = ""
                        revision.structured_value_text = "{}"
                        revision.evidence_refs_text = "[]"
                session.add(MemoryEventModel(**_memory_event_values(event)))
            await session.commit()
            await session.refresh(row)
            return _memory_from_row(row), event, False

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[MemoryItem]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryItemModel)
                        .where(
                            MemoryItemModel.lifecycle_status == "active",
                            MemoryItemModel.ttl_expires_at.is_not(None),
                            MemoryItemModel.ttl_expires_at <= now,
                        )
                        .order_by(MemoryItemModel.ttl_expires_at, MemoryItemModel.memory_id)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_memory_from_row(row) for row in rows]

    async def get_item(self, memory_id: str) -> MemoryItem | None:
        async with self.session_factory() as session:
            row = await session.get(MemoryItemModel, memory_id)
            return _memory_from_row(row) if row is not None else None

    async def complete_delete_from_index(
        self,
        index_operation: MemoryIndexOperation,
        *,
        now: datetime,
        provider_status: str | None = None,
    ) -> tuple[MemoryEvent, bool]:
        event_id = _index_event_id(index_operation, "delete-completed")
        async with self.session_factory() as session:
            async with session.begin():
                existing = await session.get(MemoryEventModel, event_id)
                if existing is not None:
                    return _memory_event_from_row(existing), True
                index_row = await session.get(
                    MemoryIndexOperationModel, index_operation.index_operation_id
                )
                _validate_delete_index_completion(
                    _operation_from_row(index_row) if index_row is not None else None,
                    index_operation,
                )
                item_row = await session.get(MemoryItemModel, index_operation.memory_id)
                item = _memory_from_row(item_row) if item_row is not None else None
                _validate_delete_index_item(item, index_operation)
                request_row = await session.scalar(
                    select(MemoryEventModel)
                    .where(
                        MemoryEventModel.memory_id == index_operation.memory_id,
                        MemoryEventModel.event_type == "memory_deletion_requested",
                    )
                    .order_by(MemoryEventModel.created_at.desc())
                    .limit(1)
                )
                request = _memory_event_from_row(request_row) if request_row is not None else None
                tombstone = _delete_index_event(
                    index_operation,
                    item=item,
                    request=request,
                    event_id=event_id,
                    event_type="memory_deleted_tombstone",
                    provider_status=_delete_provider_status(index_operation, provider_status),
                    now=now,
                )
                await session.execute(
                    delete(MemoryRevisionModel).where(
                        MemoryRevisionModel.memory_id == index_operation.memory_id
                    )
                )
                await session.delete(item_row)
                session.add(MemoryEventModel(**_memory_event_values(tombstone)))
            return tombstone, False

    async def record_delete_index_failure(
        self,
        index_operation: MemoryIndexOperation,
        *,
        error_code: str,
        dead_letter: bool,
        now: datetime,
    ) -> tuple[MemoryItem, MemoryEvent, bool]:
        suffix = (
            f"delete-{'dead-letter' if dead_letter else 'retry'}:{index_operation.attempt_count}"
        )
        event_id = _index_event_id(index_operation, suffix)
        async with self.session_factory() as session:
            existing = await session.get(MemoryEventModel, event_id)
            row = await session.scalar(
                select(MemoryItemModel)
                .where(
                    MemoryItemModel.memory_id == index_operation.memory_id,
                    MemoryItemModel.tenant_id == index_operation.tenant_id,
                )
                .with_for_update()
            )
            item = _memory_from_row(row) if row is not None else None
            _validate_delete_index_item(item, index_operation)
            if existing is not None:
                return item, _memory_event_from_row(existing), True
            request_row = await session.scalar(
                select(MemoryEventModel)
                .where(
                    MemoryEventModel.memory_id == index_operation.memory_id,
                    MemoryEventModel.event_type == "memory_deletion_requested",
                )
                .order_by(MemoryEventModel.created_at.desc())
                .limit(1)
            )
            request = _memory_event_from_row(request_row) if request_row is not None else None
            event = _delete_index_event(
                index_operation,
                item=item,
                request=request,
                event_id=event_id,
                event_type=(
                    "memory_deletion_dead_letter" if dead_letter else "memory_deletion_retry"
                ),
                provider_status="dead_letter" if dead_letter else "retry",
                now=now,
                error_code=error_code,
            )
            row.index_status = "dead_letter" if dead_letter else "deletion_pending"
            row.updated_at = now
            if dead_letter:
                values = _scrubbed_item_values(item)
                row.content = values["content"]
                row.structured_value_text = dumps(values["structured_value"])
                row.metadata_text = dumps(values["metadata"])
                row.candidate_hash = values["candidate_hash"]
                row.canonical_refs_text = dumps(values["canonical_refs"])
                row.formation_job_id = values["formation_job_id"]
                revisions = (
                    (
                        await session.execute(
                            select(MemoryRevisionModel).where(
                                MemoryRevisionModel.memory_id == index_operation.memory_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for revision in revisions:
                    revision.content = ""
                    revision.structured_value_text = "{}"
                    revision.evidence_refs_text = "[]"
            session.add(MemoryEventModel(**_memory_event_values(event)))
            await session.commit()
            await session.refresh(row)
            return _memory_from_row(row), event, False

    async def _load_delete_replay(
        self,
        *,
        operation: MemoryLifecycleOperation,
        expected_event: MemoryEvent,
        expected_index: MemoryIndexOperation,
    ) -> tuple[MemoryItem | None, MemoryEvent, MemoryIndexOperation] | None:
        async with self.session_factory() as session:
            event_row = await session.get(MemoryEventModel, expected_event.event_id)
            if event_row is None:
                return None
            index_row = await session.scalar(
                select(MemoryIndexOperationModel).where(
                    MemoryIndexOperationModel.idempotency_key == expected_index.idempotency_key
                )
            )
            if index_row is None:
                raise ValueError("Incomplete deletion replay state")
            stored_event = _memory_event_from_row(event_row)
            stored_index = _operation_from_row(index_row)
            _validate_event_replay(stored_event, expected_event)
            _validate_index_replay(stored_index, expected_index)
            item_row = await session.get(MemoryItemModel, _required_memory_id(operation))
            item = _memory_from_row(item_row) if item_row is not None else None
            if item is not None:
                _validate_operation_target(item, operation, require_active=False)
                if item.current_revision_id != operation.revision_id:
                    raise ValueError("Memory lifecycle replay identity conflict")
            elif stored_index.status != "completed":
                raise ValueError("Incomplete deletion replay state")
            return item, stored_event, stored_index

    async def _wait_for_delete_replay(
        self,
        *,
        operation: MemoryLifecycleOperation,
        expected_event: MemoryEvent,
        expected_index: MemoryIndexOperation,
    ) -> tuple[MemoryItem | None, MemoryEvent, MemoryIndexOperation] | None:
        for _ in range(20):
            replay = await self._load_delete_replay(
                operation=operation,
                expected_event=expected_event,
                expected_index=expected_index,
            )
            if replay is not None:
                return replay
            await asyncio.sleep(0.01)
        return None

    async def _load_event_replay(self, expected: MemoryEvent) -> MemoryEvent | None:
        async with self.session_factory() as session:
            row = await session.get(MemoryEventModel, expected.event_id)
            if row is None:
                return None
            stored = _memory_event_from_row(row)
            _validate_event_replay(stored, expected)
            return stored

    async def _wait_for_event_replay(self, expected: MemoryEvent) -> MemoryEvent | None:
        for _ in range(20):
            replay = await self._load_event_replay(expected)
            if replay is not None:
                return replay
            await asyncio.sleep(0.01)
        return None

    async def _load_replay(
        self,
        *,
        expected_event: MemoryEvent,
        expected_revision: MemoryRevision,
        expected_index: MemoryIndexOperation,
        expected_item: MemoryItem | None = None,
        expected_operation: MemoryLifecycleOperation | None = None,
    ):
        async with self.session_factory() as session:
            event = await session.get(MemoryEventModel, expected_event.event_id)
            if event is None:
                return None
            memory_id = expected_revision.memory_id
            item = await session.get(MemoryItemModel, memory_id)
            revision = await session.get(MemoryRevisionModel, expected_revision.revision_id)
            index = await session.scalar(
                select(MemoryIndexOperationModel).where(
                    MemoryIndexOperationModel.memory_id == memory_id,
                    MemoryIndexOperationModel.revision_id == expected_revision.revision_id,
                )
            )
            if item is None or revision is None or index is None:
                raise ValueError("Incomplete lifecycle replay state")
            stored_item = _memory_from_row(item)
            stored_revision = _revision_from_row(revision)
            stored_event = _memory_event_from_row(event)
            stored_index = _operation_from_row(index)
            _validate_event_replay(stored_event, expected_event)
            _validate_revision_replay(stored_revision, expected_revision)
            _validate_index_replay(stored_index, expected_index)
            if expected_item is not None:
                _validate_item_replay(stored_item, expected_item)
            elif expected_operation is not None:
                _validate_operation_target(stored_item, expected_operation, require_active=False)
            return (
                stored_item.model_copy(update={"current_revision_no": revision.revision_no}),
                stored_revision,
                stored_event,
                stored_index,
            )

    async def _scrub_pending_payloads(
        self, session: AsyncSession, operation: MemoryLifecycleOperation
    ) -> None:
        memory_id = _required_memory_id(operation)
        turns = (
            (
                await session.execute(
                    select(MemoryFormationTurnModel).where(
                        MemoryFormationTurnModel.tenant_id == operation.tenant_id,
                        MemoryFormationTurnModel.user_id == operation.user_id,
                        MemoryFormationTurnModel.status.in_(["pending", "claimed"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        matched_turns: set[str] = set()
        for turn in turns:
            used_ids = loads(turn.used_memory_ids_text, [])
            if turn.turn_id in operation.canonical_refs or memory_id in used_ids:
                matched_turns.add(turn.turn_id)
                turn.user_text = ""
                turn.assistant_text = ""
                turn.used_memory_ids_text = dumps(
                    [value for value in used_ids if value != memory_id]
                )
        jobs = (
            (
                await session.execute(
                    select(MemoryFormationJobModel).where(
                        MemoryFormationJobModel.tenant_id == operation.tenant_id,
                        MemoryFormationJobModel.user_id == operation.user_id,
                        MemoryFormationJobModel.status.in_(["pending", "claimed", "retry"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            refs = set(loads(job.source_refs_text, []))
            summary = loads(job.trace_summary_text, {})
            if matched_turns.intersection(refs) or _payload_contains(summary, memory_id):
                job.trace_summary_text = dumps(_scrub_payload(summary))
        events = (
            (
                await session.execute(
                    select(MemoryEventModel).where(
                        MemoryEventModel.memory_id == memory_id,
                        MemoryEventModel.tenant_id == operation.tenant_id,
                        MemoryEventModel.user_id == operation.user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            event.payload_text = dumps(_scrub_payload(loads(event.payload_text, {})))


def _copy_commit(item, revision, event, index, replay):
    return (
        item.model_copy(deep=True),
        revision.model_copy(deep=True),
        event.model_copy(deep=True),
        index.model_copy(deep=True),
        replay,
    )


def _validate_active_key_available(items, incoming: MemoryItem) -> None:
    if any(
        item.lifecycle_status == MemoryLifecycleStatus.ACTIVE
        and item.tenant_id == incoming.tenant_id
        and item.subject_type == incoming.subject_type
        and item.subject_id == incoming.subject_id
        and str(item.scope) == str(incoming.scope)
        and item.memory_key == incoming.memory_key
        for item in items
    ):
        raise ValueError("Active memory key already exists")


def _validate_index_identity(repository, incoming: MemoryIndexOperation) -> None:
    existing_id = repository.by_idempotency.get(incoming.idempotency_key)
    if existing_id is None:
        return
    existing = repository.operations[existing_id]
    if (
        existing.memory_id,
        existing.revision_id,
        existing.operation,
        existing.tenant_id,
    ) != (
        incoming.memory_id,
        incoming.revision_id,
        incoming.operation,
        incoming.tenant_id,
    ):
        raise ValueError("Index operation identity conflict")


def _validate_item_replay(existing: MemoryItem, incoming: MemoryItem) -> None:
    if (
        existing.memory_id,
        existing.tenant_id,
        existing.user_id,
        existing.subject_type,
        existing.subject_id,
        existing.agent_id,
        str(existing.scope),
        existing.memory_key,
        existing.source,
        existing.metadata,
    ) != (
        incoming.memory_id,
        incoming.tenant_id,
        incoming.user_id,
        incoming.subject_type,
        incoming.subject_id,
        incoming.agent_id,
        str(incoming.scope),
        incoming.memory_key,
        incoming.source,
        incoming.metadata,
    ):
        raise ValueError("Memory lifecycle replay identity conflict")


def _validate_event_replay(existing: MemoryEvent, incoming: MemoryEvent) -> None:
    if (
        existing.event_id,
        existing.event_type,
        existing.memory_id,
        existing.tenant_id,
        existing.user_id,
        existing.agent_id,
        existing.formation_job_id,
        existing.memory_key,
        existing.decision_status,
        existing.scope,
        existing.payload,
    ) != (
        incoming.event_id,
        incoming.event_type,
        incoming.memory_id,
        incoming.tenant_id,
        incoming.user_id,
        incoming.agent_id,
        incoming.formation_job_id,
        incoming.memory_key,
        incoming.decision_status,
        incoming.scope,
        incoming.payload,
    ):
        raise ValueError("Memory lifecycle replay identity conflict")


def _validate_revision_replay(existing: MemoryRevision, incoming: MemoryRevision) -> None:
    if (
        existing.revision_id,
        existing.memory_id,
        existing.memory_key,
        existing.operation,
        existing.content,
        existing.structured_value,
        existing.evidence_refs,
        existing.confidence,
        existing.policy_version,
        existing.formation_job_id,
    ) != (
        incoming.revision_id,
        incoming.memory_id,
        incoming.memory_key,
        incoming.operation,
        incoming.content,
        incoming.structured_value,
        incoming.evidence_refs,
        incoming.confidence,
        incoming.policy_version,
        incoming.formation_job_id,
    ):
        raise ValueError("Memory lifecycle replay identity conflict")


def _validate_index_replay(existing: MemoryIndexOperation, incoming: MemoryIndexOperation) -> None:
    if (
        existing.index_operation_id,
        existing.idempotency_key,
        existing.operation,
        existing.memory_id,
        existing.revision_id,
        existing.tenant_id,
    ) != (
        incoming.index_operation_id,
        incoming.idempotency_key,
        incoming.operation,
        incoming.memory_id,
        incoming.revision_id,
        incoming.tenant_id,
    ):
        raise ValueError("Memory lifecycle replay identity conflict")


def _validate_delete_index_completion(
    stored: MemoryIndexOperation | None, incoming: MemoryIndexOperation
) -> None:
    if (
        stored is None
        or stored.index_operation_id != incoming.index_operation_id
        or stored.operation != "delete"
        or stored.status != "completed"
        or stored.memory_id != incoming.memory_id
        or stored.tenant_id != incoming.tenant_id
        or stored.revision_id != incoming.revision_id
    ):
        raise ValueError("Completed delete index operation required")


def _validate_delete_index_item(
    item: MemoryItem | None, index_operation: MemoryIndexOperation
) -> None:
    if (
        item is None
        or item.memory_id != index_operation.memory_id
        or item.tenant_id != index_operation.tenant_id
        or item.lifecycle_status != MemoryLifecycleStatus.DELETION_PENDING
        or item.current_revision_id != index_operation.revision_id
    ):
        raise ValueError("Memory deletion is not pending")


def _index_event_id(index_operation: MemoryIndexOperation, suffix: str) -> str:
    value = f"{index_operation.index_operation_id}:{suffix}"
    return f"mevt_{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _delete_provider_status(
    index_operation: MemoryIndexOperation, explicit_status: str | None
) -> str:
    stored = index_operation.last_error_metadata.get("provider_status")
    if explicit_status:
        return explicit_status
    if isinstance(stored, str) and stored in {"success", "not_found"}:
        return stored
    return "completed"


def _delete_request_event(events: list[MemoryEvent], memory_id: str) -> MemoryEvent | None:
    return next(
        (
            event
            for event in reversed(events)
            if event.memory_id == memory_id and event.event_type == "memory_deletion_requested"
        ),
        None,
    )


def _delete_index_event(
    index_operation: MemoryIndexOperation,
    *,
    item: MemoryItem,
    request: MemoryEvent | None,
    event_id: str,
    event_type: str,
    provider_status: str,
    now: datetime,
    error_code: str | None = None,
) -> MemoryEvent:
    request_payload = request.payload if request is not None else {}
    payload = {
        "operation": "delete",
        "provider_status": provider_status,
        "reason_code": request_payload.get("reason_code", "provider_error"),
        "target_hash": request_payload.get(
            "target_hash",
            f"sha256:{hashlib.sha256(index_operation.memory_id.encode()).hexdigest()}",
        ),
    }
    if error_code:
        payload["error_code"] = error_code[:128]
    return MemoryEvent(
        event_id=event_id,
        event_type=event_type,
        memory_id=index_operation.memory_id,
        user_id=item.user_id,
        tenant_id=index_operation.tenant_id,
        agent_id=item.agent_id,
        formation_job_id=item.formation_job_id,
        decision_status=request.decision_status if request is not None else None,
        scope=str(item.scope),
        payload=payload,
        created_at=now,
    )


def _validate_operation_target(
    item: MemoryItem | None,
    operation: MemoryLifecycleOperation,
    *,
    require_active: bool,
) -> None:
    if item is None:
        raise ValueError("Memory lifecycle target not found")
    if (
        item.tenant_id != operation.tenant_id
        or item.user_id != operation.user_id
        or item.subject_type != operation.subject_type
        or item.subject_id != operation.subject_id
        or not _memory_key_matches(item.memory_key, operation.memory_key)
    ):
        raise ValueError("Memory lifecycle target not found")
    if require_active and item.lifecycle_status != MemoryLifecycleStatus.ACTIVE:
        raise ValueError("Memory lifecycle target is not active")


def _required_memory_id(operation: MemoryLifecycleOperation) -> str:
    if not operation.memory_id:
        raise ValueError("Memory lifecycle operation requires memory_id")
    return operation.memory_id


def _memory_key_matches(stored: str | None, expected: str) -> bool:
    return stored == expected or (stored is None and expected.startswith("legacy-memory:"))


def _database_memory_key_condition(operation: MemoryLifecycleOperation):
    if operation.memory_key.startswith("legacy-memory:"):
        return MemoryItemModel.memory_key.is_(None)
    return MemoryItemModel.memory_key == operation.memory_key


def _scrubbed_item_values(item: MemoryItem) -> dict:
    external_id = item.metadata.get("mem0_memory_id")
    return {
        "content": "",
        "structured_value": {},
        "metadata": {"mem0_memory_id": external_id} if isinstance(external_id, str) else {},
        "candidate_hash": None,
        "canonical_refs": [],
        "formation_job_id": None,
    }


def _scrub_revision(revision: MemoryRevision) -> MemoryRevision:
    return revision.model_copy(
        deep=True, update={"content": "", "structured_value": {}, "evidence_refs": []}
    )


def _scrub_payload(value):
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if is_sensitive_key(key)
                or key.lower()
                in {
                    "content",
                    "content_preview",
                    "quote",
                    "prompt",
                    "user_text",
                    "assistant_text",
                    "structured_value",
                    "pending_candidate",
                }
                else _scrub_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_payload(item) for item in value]
    return value


def _payload_contains(value, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_payload_contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_payload_contains(item, expected) for item in value)
    return value == expected


def _validate_completed_delete_index(
    index: MemoryIndexOperation | None, operation: MemoryLifecycleOperation
) -> None:
    if (
        index is None
        or index.operation != "delete"
        or index.status != "completed"
        or index.memory_id != operation.memory_id
        or index.revision_id != operation.revision_id
        or index.tenant_id != operation.tenant_id
    ):
        raise ValueError("Provider delete operation is not completed")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["DatabaseMemoryLifecycleStore", "MemoryLifecycleStore"]
