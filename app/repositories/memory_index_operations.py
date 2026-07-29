import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import MemoryIndexOperationModel
from app.repositories.json_utils import dumps, loads
from app.schemas.memory import (
    MemoryIndexOperation,
    MemoryIndexOperationStatus,
)


class MemoryIndexOutboxRepository:
    def __init__(self) -> None:
        self.operations: dict[str, MemoryIndexOperation] = {}
        self.by_idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def add(self, operation: MemoryIndexOperation) -> MemoryIndexOperation:
        async with self._lock:
            existing_id = self.by_idempotency.get(operation.idempotency_key)
            if existing_id is not None:
                existing = self.operations[existing_id]
                _validate_operation_identity(existing, operation)
                return existing.model_copy(deep=True)
            stored = operation.model_copy(deep=True)
            self.operations[operation.index_operation_id] = stored
            self.by_idempotency[operation.idempotency_key] = operation.index_operation_id
            return stored.model_copy(deep=True)

    async def get(self, index_operation_id: str, *, tenant_id: str) -> MemoryIndexOperation | None:
        operation = self.operations.get(index_operation_id)
        if operation is None or operation.tenant_id != tenant_id:
            return None
        return operation.model_copy(deep=True)

    async def list_for_memory(
        self, memory_id: str, *, tenant_id: str, limit: int = 100
    ) -> list[MemoryIndexOperation]:
        values = [
            operation
            for operation in self.operations.values()
            if operation.memory_id == memory_id and operation.tenant_id == tenant_id
        ]
        return [
            operation.model_copy(deep=True)
            for operation in sorted(values, key=lambda item: item.updated_at, reverse=True)[:limit]
        ]

    async def latest_deletes_for_memories(
        self, memory_ids: list[str], *, tenant_id: str
    ) -> dict[str, MemoryIndexOperation]:
        requested = set(memory_ids)
        latest: dict[str, MemoryIndexOperation] = {}
        operations = sorted(
            self.operations.values(),
            key=lambda item: (item.updated_at, item.index_operation_id),
            reverse=True,
        )
        for operation in operations:
            if (
                operation.tenant_id == tenant_id
                and operation.memory_id in requested
                and operation.operation == "delete"
                and operation.memory_id not in latest
            ):
                latest[operation.memory_id] = operation.model_copy(deep=True)
        return latest

    async def accept_governance_repair(
        self,
        index_operation_id: str,
        *,
        tenant_id: str,
        expected_status: MemoryIndexOperationStatus,
        idempotency_key: str,
        now: datetime,
        requeue: bool,
        expected_version: str | None = None,
        expected_anomaly: str | None = None,
    ) -> tuple[MemoryIndexOperation, bool]:
        async with self._lock:
            operation = self.operations.get(index_operation_id)
            if operation is None or operation.tenant_id != tenant_id:
                raise ValueError("Governance repair target not found")
            previous_key = operation.last_error_metadata.get("governance_repair_key")
            if previous_key == idempotency_key:
                if (
                    operation.last_error_metadata.get("governance_expected_version")
                    != expected_version
                    or operation.last_error_metadata.get("governance_expected_anomaly")
                    != expected_anomaly
                ):
                    raise ValueError("Governance repair request identity changed")
                return operation.model_copy(deep=True), True
            if not requeue and previous_key:
                raise ValueError("Canonical governance repair is already claimed")
            if operation.status != expected_status:
                raise ValueError("Governance repair status changed")
            metadata = {
                **operation.last_error_metadata,
                "governance_repair_key": idempotency_key,
                "governance_expected_version": expected_version,
                "governance_expected_anomaly": expected_anomaly,
            }
            updates = {"last_error_metadata": metadata, "updated_at": now}
            if requeue:
                updates.update(
                    {
                        "status": MemoryIndexOperationStatus.PENDING,
                        "attempt_count": 0,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                        "next_attempt_at": None,
                        "last_error_code": None,
                    }
                )
            stored = operation.model_copy(deep=True, update=updates)
            self.operations[index_operation_id] = stored
            return stored.model_copy(deep=True), False

    async def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: float,
        tenant_id: str | None = None,
    ) -> MemoryIndexOperation | None:
        async with self._lock:
            for operation in list(self.operations.values()):
                if (
                    (tenant_id is None or operation.tenant_id == tenant_id)
                    and operation.status == MemoryIndexOperationStatus.CLAIMED
                    and operation.lease_expires_at is not None
                    and operation.lease_expires_at <= now
                    and operation.attempt_count >= operation.max_attempts
                ):
                    self.operations[operation.index_operation_id] = operation.model_copy(
                        update={
                            "status": MemoryIndexOperationStatus.DEAD_LETTER,
                            "lease_owner": None,
                            "lease_token": None,
                            "lease_expires_at": None,
                            "last_error_code": "lease_expired_attempts_exhausted",
                            "updated_at": now,
                        }
                    )
            eligible = [
                operation
                for operation in self.operations.values()
                if (tenant_id is None or operation.tenant_id == tenant_id)
                and operation.attempt_count < operation.max_attempts
                and (
                    operation.status == MemoryIndexOperationStatus.PENDING
                    or (
                        operation.status == MemoryIndexOperationStatus.RETRY
                        and (operation.next_attempt_at is None or operation.next_attempt_at <= now)
                    )
                    or (
                        operation.status == MemoryIndexOperationStatus.CLAIMED
                        and operation.lease_expires_at is not None
                        and operation.lease_expires_at <= now
                    )
                )
            ]
            if not eligible:
                return None
            operation = sorted(
                eligible, key=lambda item: (item.created_at, item.index_operation_id)
            )[0]
            claimed = operation.model_copy(
                update={
                    "status": MemoryIndexOperationStatus.CLAIMED,
                    "attempt_count": operation.attempt_count + 1,
                    "lease_owner": owner,
                    "lease_token": f"lease_{uuid4().hex}",
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            self.operations[operation.index_operation_id] = claimed
            return claimed.model_copy(deep=True)

    async def complete(
        self,
        index_operation_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        external_memory_id: str | None = None,
        result_metadata: dict | None = None,
    ) -> MemoryIndexOperation:
        async with self._lock:
            operation = self._claimed(
                index_operation_id, owner=owner, lease_token=lease_token, now=now
            )
            completed = operation.model_copy(
                update={
                    "status": MemoryIndexOperationStatus.COMPLETED,
                    "external_memory_id": external_memory_id or operation.external_memory_id,
                    "last_error_metadata": {
                        **operation.last_error_metadata,
                        **(result_metadata or {}),
                    },
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            )
            self.operations[index_operation_id] = completed
            return completed.model_copy(deep=True)

    async def fail(
        self,
        index_operation_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        next_attempt_at: datetime,
    ) -> MemoryIndexOperation:
        async with self._lock:
            operation = self._claimed(
                index_operation_id, owner=owner, lease_token=lease_token, now=now
            )
            status = (
                MemoryIndexOperationStatus.DEAD_LETTER
                if operation.attempt_count >= operation.max_attempts
                else MemoryIndexOperationStatus.RETRY
            )
            failed = operation.model_copy(
                update={
                    "status": status,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "next_attempt_at": next_attempt_at,
                    "last_error_code": error_code,
                    "updated_at": now,
                }
            )
            self.operations[index_operation_id] = failed
            return failed.model_copy(deep=True)

    async def list_repair_candidates(
        self, *, tenant_id: str, statuses: list[str], limit: int = 100
    ) -> list[MemoryIndexOperation]:
        allowed = set(statuses)
        values = [
            operation
            for operation in self.operations.values()
            if operation.tenant_id == tenant_id and str(operation.status) in allowed
        ]
        return [
            operation.model_copy(deep=True)
            for operation in sorted(values, key=lambda item: item.updated_at)[:limit]
        ]

    def _claimed(
        self, index_operation_id: str, *, owner: str, lease_token: str, now: datetime
    ) -> MemoryIndexOperation:
        operation = self.operations.get(index_operation_id)
        if operation is None:
            raise ValueError("Index operation not found")
        if operation.status != MemoryIndexOperationStatus.CLAIMED:
            raise ValueError("Index operation is not claimed")
        if operation.lease_owner != owner or operation.lease_token != lease_token:
            raise ValueError("Index operation lease ownership changed")
        if operation.lease_expires_at is None or operation.lease_expires_at <= now:
            raise ValueError("Index operation lease expired")
        return operation


class DatabaseMemoryIndexOutboxRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add(self, operation: MemoryIndexOperation) -> MemoryIndexOperation:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(MemoryIndexOperationModel).where(
                    MemoryIndexOperationModel.idempotency_key == operation.idempotency_key
                )
            )
            if existing is not None:
                stored = _operation_from_row(existing)
                _validate_operation_identity(stored, operation)
                return stored
            row = MemoryIndexOperationModel(**_operation_values(operation))
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(MemoryIndexOperationModel).where(
                        MemoryIndexOperationModel.idempotency_key == operation.idempotency_key
                    )
                )
                if existing is None:
                    raise
                stored = _operation_from_row(existing)
                _validate_operation_identity(stored, operation)
                return stored
            return operation

    async def get(self, index_operation_id: str, *, tenant_id: str) -> MemoryIndexOperation | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(MemoryIndexOperationModel).where(
                    MemoryIndexOperationModel.index_operation_id == index_operation_id,
                    MemoryIndexOperationModel.tenant_id == tenant_id,
                )
            )
            return _operation_from_row(row) if row is not None else None

    async def list_for_memory(
        self, memory_id: str, *, tenant_id: str, limit: int = 100
    ) -> list[MemoryIndexOperation]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryIndexOperationModel)
                        .where(
                            MemoryIndexOperationModel.memory_id == memory_id,
                            MemoryIndexOperationModel.tenant_id == tenant_id,
                        )
                        .order_by(MemoryIndexOperationModel.updated_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_operation_from_row(row) for row in rows]

    async def latest_deletes_for_memories(
        self, memory_ids: list[str], *, tenant_id: str
    ) -> dict[str, MemoryIndexOperation]:
        if not memory_ids:
            return {}
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryIndexOperationModel)
                        .where(
                            MemoryIndexOperationModel.memory_id.in_(memory_ids),
                            MemoryIndexOperationModel.tenant_id == tenant_id,
                            MemoryIndexOperationModel.operation == "delete",
                        )
                        .order_by(
                            MemoryIndexOperationModel.updated_at.desc(),
                            MemoryIndexOperationModel.index_operation_id.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[str, MemoryIndexOperation] = {}
        for row in rows:
            if row.memory_id not in latest:
                latest[row.memory_id] = _operation_from_row(row)
        return latest

    async def accept_governance_repair(
        self,
        index_operation_id: str,
        *,
        tenant_id: str,
        expected_status: MemoryIndexOperationStatus,
        idempotency_key: str,
        now: datetime,
        requeue: bool,
        expected_version: str | None = None,
        expected_anomaly: str | None = None,
    ) -> tuple[MemoryIndexOperation, bool]:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(MemoryIndexOperationModel)
                .where(
                    MemoryIndexOperationModel.index_operation_id == index_operation_id,
                    MemoryIndexOperationModel.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("Governance repair target not found")
            operation = _operation_from_row(row)
            previous_key = operation.last_error_metadata.get("governance_repair_key")
            if previous_key == idempotency_key:
                if (
                    operation.last_error_metadata.get("governance_expected_version")
                    != expected_version
                    or operation.last_error_metadata.get("governance_expected_anomaly")
                    != expected_anomaly
                ):
                    raise ValueError("Governance repair request identity changed")
                return operation, True
            if not requeue and previous_key:
                raise ValueError("Canonical governance repair is already claimed")
            if operation.status != expected_status:
                raise ValueError("Governance repair status changed")
            metadata = {
                **operation.last_error_metadata,
                "governance_repair_key": idempotency_key,
                "governance_expected_version": expected_version,
                "governance_expected_anomaly": expected_anomaly,
            }
            row.last_error_metadata_text = dumps(metadata)
            row.updated_at = now
            if requeue:
                row.status = "pending"
                row.attempt_count = 0
                row.lease_owner = None
                row.lease_token = None
                row.lease_expires_at = None
                row.next_attempt_at = None
                row.last_error_code = None
            await session.commit()
            await session.refresh(row)
            return _operation_from_row(row), False

    async def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: float,
        tenant_id: str | None = None,
    ) -> MemoryIndexOperation | None:
        async with self.session_factory() as session:
            expired_claims = update(MemoryIndexOperationModel).where(
                MemoryIndexOperationModel.status == "claimed",
                MemoryIndexOperationModel.lease_expires_at <= now,
                MemoryIndexOperationModel.attempt_count >= MemoryIndexOperationModel.max_attempts,
            )
            if tenant_id is not None:
                expired_claims = expired_claims.where(
                    MemoryIndexOperationModel.tenant_id == tenant_id
                )
            await session.execute(
                expired_claims.values(
                    status="dead_letter",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code="lease_expired_attempts_exhausted",
                    updated_at=now,
                )
            )
            eligible = select(MemoryIndexOperationModel).where(
                MemoryIndexOperationModel.attempt_count < MemoryIndexOperationModel.max_attempts,
                or_(
                    MemoryIndexOperationModel.status == "pending",
                    (
                        (MemoryIndexOperationModel.status == "retry")
                        & or_(
                            MemoryIndexOperationModel.next_attempt_at.is_(None),
                            MemoryIndexOperationModel.next_attempt_at <= now,
                        )
                    ),
                    (
                        (MemoryIndexOperationModel.status == "claimed")
                        & (MemoryIndexOperationModel.lease_expires_at <= now)
                    ),
                ),
            )
            if tenant_id is not None:
                eligible = eligible.where(MemoryIndexOperationModel.tenant_id == tenant_id)
            row = await session.scalar(
                eligible.order_by(
                    MemoryIndexOperationModel.created_at,
                    MemoryIndexOperationModel.index_operation_id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                await session.commit()
                return None
            token = f"lease_{uuid4().hex}"
            result = await session.execute(
                update(MemoryIndexOperationModel)
                .where(
                    MemoryIndexOperationModel.index_operation_id == row.index_operation_id,
                    MemoryIndexOperationModel.status == row.status,
                    MemoryIndexOperationModel.attempt_count == row.attempt_count,
                )
                .values(
                    status="claimed",
                    attempt_count=row.attempt_count + 1,
                    lease_owner=owner,
                    lease_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            await session.refresh(row)
            return _operation_from_row(row)

    async def complete(
        self,
        index_operation_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        external_memory_id: str | None = None,
        result_metadata: dict | None = None,
    ) -> MemoryIndexOperation:
        values = {"status": "completed"}
        if external_memory_id is not None:
            values["external_memory_id"] = external_memory_id
        if result_metadata is not None:
            values["last_error_metadata_text"] = dumps(result_metadata)
        return await self._terminal_update(
            index_operation_id,
            owner=owner,
            lease_token=lease_token,
            now=now,
            values=values,
        )

    async def fail(
        self,
        index_operation_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        next_attempt_at: datetime,
    ) -> MemoryIndexOperation:
        async with self.session_factory() as session:
            row = await session.get(MemoryIndexOperationModel, index_operation_id)
            if row is None:
                raise ValueError("Index operation not found")
            status = "dead_letter" if row.attempt_count >= row.max_attempts else "retry"
        return await self._terminal_update(
            index_operation_id,
            owner=owner,
            lease_token=lease_token,
            now=now,
            values={
                "status": status,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code,
            },
        )

    async def list_repair_candidates(
        self, *, tenant_id: str, statuses: list[str], limit: int = 100
    ) -> list[MemoryIndexOperation]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryIndexOperationModel)
                        .where(
                            MemoryIndexOperationModel.tenant_id == tenant_id,
                            MemoryIndexOperationModel.status.in_(statuses),
                        )
                        .order_by(MemoryIndexOperationModel.updated_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_operation_from_row(row) for row in rows]

    async def _terminal_update(
        self,
        index_operation_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        values: dict,
    ) -> MemoryIndexOperation:
        async with self.session_factory() as session:
            result = await session.execute(
                update(MemoryIndexOperationModel)
                .where(
                    MemoryIndexOperationModel.index_operation_id == index_operation_id,
                    MemoryIndexOperationModel.status == "claimed",
                    MemoryIndexOperationModel.lease_owner == owner,
                    MemoryIndexOperationModel.lease_token == lease_token,
                    MemoryIndexOperationModel.lease_expires_at > now,
                )
                .values(
                    **values,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise ValueError("Index operation claim transition lost")
            await session.commit()
            row = await session.get(MemoryIndexOperationModel, index_operation_id)
            return _operation_from_row(row)


def _validate_operation_identity(
    existing: MemoryIndexOperation, incoming: MemoryIndexOperation
) -> None:
    if (
        existing.tenant_id,
        existing.memory_id,
        existing.revision_id,
        existing.operation,
    ) != (
        incoming.tenant_id,
        incoming.memory_id,
        incoming.revision_id,
        incoming.operation,
    ):
        raise ValueError("Index operation identity conflict")


def _operation_values(operation: MemoryIndexOperation) -> dict:
    return {
        "index_operation_id": operation.index_operation_id,
        "idempotency_key": operation.idempotency_key,
        "operation": operation.operation.value,
        "memory_id": operation.memory_id,
        "revision_id": operation.revision_id,
        "tenant_id": operation.tenant_id,
        "external_memory_id": operation.external_memory_id,
        "status": operation.status.value,
        "attempt_count": operation.attempt_count,
        "max_attempts": operation.max_attempts,
        "lease_owner": operation.lease_owner,
        "lease_token": operation.lease_token,
        "lease_expires_at": operation.lease_expires_at,
        "next_attempt_at": operation.next_attempt_at,
        "last_error_code": operation.last_error_code,
        "last_error_metadata_text": dumps(operation.last_error_metadata),
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
    }


def _operation_from_row(row) -> MemoryIndexOperation:
    return MemoryIndexOperation(
        index_operation_id=row.index_operation_id,
        idempotency_key=row.idempotency_key,
        operation=row.operation,
        memory_id=row.memory_id,
        revision_id=row.revision_id,
        tenant_id=row.tenant_id,
        external_memory_id=row.external_memory_id,
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        next_attempt_at=row.next_attempt_at,
        last_error_code=row.last_error_code,
        last_error_metadata=loads(row.last_error_metadata_text, {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
