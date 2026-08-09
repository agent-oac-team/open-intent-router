import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import TurnOutboxModel
from app.repositories.json_utils import dumps, loads
from app.schemas.turns import TurnOutboxEvent


class OutboxClaimConflict(ValueError):
    pass


class MemoryTurnOutboxRepository:
    def __init__(self) -> None:
        self.events: dict[str, TurnOutboxEvent] = {}
        self.idempotency_keys: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def add_idempotent(self, event: TurnOutboxEvent) -> tuple[TurnOutboxEvent, bool]:
        async with self._lock:
            existing_id = self.idempotency_keys.get(event.idempotency_key)
            if existing_id:
                return self.events[existing_id].model_copy(deep=True), False
            stored = event.model_copy(deep=True)
            self.events[event.outbox_id] = stored
            self.idempotency_keys[event.idempotency_key] = event.outbox_id
            return stored.model_copy(deep=True), True

    async def claim(self, *, owner: str, now, lease_seconds: float) -> TurnOutboxEvent | None:
        async with self._lock:
            candidates = [
                event
                for event in self.events.values()
                if (event.status in {"pending", "retry"} and event.available_at <= now)
                or (
                    event.status == "claimed"
                    and event.lease_expires_at is not None
                    and event.lease_expires_at <= now
                )
            ]
            if not candidates:
                return None
            event = min(candidates, key=lambda item: (item.available_at, item.outbox_id))
            claimed = event.model_copy(
                update={
                    "status": "claimed",
                    "lease_owner": owner,
                    "lease_token": f"outbox_lease_{uuid4().hex}",
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            self.events[event.outbox_id] = claimed
            return claimed.model_copy(deep=True)

    async def complete(
        self, outbox_id: str, *, owner: str, lease_token: str, now
    ) -> TurnOutboxEvent:
        async with self._lock:
            event = self._claimed(outbox_id, owner=owner, lease_token=lease_token)
            if event.status == "completed":
                return event.model_copy(deep=True)
            completed = event.model_copy(
                update={
                    "status": "completed",
                    "published_at": now,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            )
            self.events[outbox_id] = completed
            return completed.model_copy(deep=True)

    async def fail(
        self,
        outbox_id: str,
        *,
        owner: str,
        lease_token: str,
        now,
        retry_at,
        error_code: str,
    ) -> TurnOutboxEvent:
        async with self._lock:
            event = self._claimed(outbox_id, owner=owner, lease_token=lease_token)
            attempts = event.attempt_count + 1
            status = "dead_letter" if attempts >= event.max_attempts else "retry"
            failed = event.model_copy(
                update={
                    "status": status,
                    "attempt_count": attempts,
                    "available_at": retry_at,
                    "last_error_code": error_code,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            )
            self.events[outbox_id] = failed
            return failed.model_copy(deep=True)

    def _claimed(self, outbox_id: str, *, owner: str, lease_token: str) -> TurnOutboxEvent:
        event = self.events.get(outbox_id)
        if event is None:
            raise OutboxClaimConflict("outbox event not found")
        if event.status == "completed":
            return event
        if (
            event.status != "claimed"
            or event.lease_owner != owner
            or event.lease_token != lease_token
        ):
            raise OutboxClaimConflict("outbox claim does not match")
        return event


class DatabaseTurnOutboxRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def add_idempotent(self, event: TurnOutboxEvent) -> tuple[TurnOutboxEvent, bool]:
        async with self.session_factory() as session:
            session.add(TurnOutboxModel(**_event_values(event)))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                row = await session.scalar(
                    select(TurnOutboxModel).where(
                        TurnOutboxModel.idempotency_key == event.idempotency_key
                    )
                )
                if row is None:
                    raise
                return _event_from_row(row), False
            return event.model_copy(deep=True), True

    async def claim(self, *, owner: str, now, lease_seconds: float) -> TurnOutboxEvent | None:
        for _ in range(3):
            async with self.session_factory() as session:
                row = await session.scalar(
                    select(TurnOutboxModel)
                    .where(_claimable(now))
                    .order_by(TurnOutboxModel.available_at, TurnOutboxModel.outbox_id)
                    .limit(1)
                )
                if row is None:
                    return None
                lease_token = f"outbox_lease_{uuid4().hex}"
                claim_statement = (
                    update(TurnOutboxModel)
                    .where(TurnOutboxModel.outbox_id == row.outbox_id, _claimable(now))
                    .values(
                        status="claimed",
                        lease_owner=owner,
                        lease_token=lease_token,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                claimed = await session.execute(claim_statement)
                if claimed.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                await session.refresh(row)
                return _event_from_row(row)
        return None

    async def complete(
        self, outbox_id: str, *, owner: str, lease_token: str, now
    ) -> TurnOutboxEvent:
        async with self.session_factory() as session:
            existing = await session.get(TurnOutboxModel, outbox_id)
            if existing is not None and existing.status == "completed":
                return _event_from_row(existing)
            complete_statement = (
                update(TurnOutboxModel)
                .where(
                    TurnOutboxModel.outbox_id == outbox_id,
                    TurnOutboxModel.status == "claimed",
                    TurnOutboxModel.lease_owner == owner,
                    TurnOutboxModel.lease_token == lease_token,
                )
                .values(
                    status="completed",
                    published_at=now,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            result = await session.execute(complete_statement)
            if result.rowcount != 1:
                await session.rollback()
                raise OutboxClaimConflict("outbox claim does not match")
            await session.commit()
            await session.refresh(existing)
            return _event_from_row(existing)

    async def fail(
        self,
        outbox_id: str,
        *,
        owner: str,
        lease_token: str,
        now,
        retry_at,
        error_code: str,
    ) -> TurnOutboxEvent:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(TurnOutboxModel).where(
                    TurnOutboxModel.outbox_id == outbox_id,
                    TurnOutboxModel.status == "claimed",
                    TurnOutboxModel.lease_owner == owner,
                    TurnOutboxModel.lease_token == lease_token,
                )
            )
            if row is None:
                raise OutboxClaimConflict("outbox claim does not match")
            attempts = row.attempt_count + 1
            row.status = "dead_letter" if attempts >= row.max_attempts else "retry"
            row.attempt_count = attempts
            row.available_at = retry_at
            row.last_error_code = error_code
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            row.updated_at = now
            await session.commit()
            await session.refresh(row)
            return _event_from_row(row)


def _claimable(now):
    return or_(
        and_(
            TurnOutboxModel.status.in_(["pending", "retry"]),
            TurnOutboxModel.available_at <= now,
        ),
        and_(
            TurnOutboxModel.status == "claimed",
            TurnOutboxModel.lease_expires_at <= now,
        ),
    )


def _event_values(event: TurnOutboxEvent) -> dict:
    return {
        "outbox_id": event.outbox_id,
        "turn_id": event.turn_id,
        "event_type": event.event_type,
        "idempotency_key": event.idempotency_key,
        "payload_text": dumps(event.payload),
        "status": event.status,
        "attempt_count": event.attempt_count,
        "max_attempts": event.max_attempts,
        "lease_owner": event.lease_owner,
        "lease_token": event.lease_token,
        "lease_expires_at": event.lease_expires_at,
        "available_at": event.available_at,
        "published_at": event.published_at,
        "last_error_code": event.last_error_code,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def _event_from_row(row: TurnOutboxModel) -> TurnOutboxEvent:
    return TurnOutboxEvent(
        outbox_id=row.outbox_id,
        turn_id=row.turn_id,
        event_type=row.event_type,
        idempotency_key=row.idempotency_key,
        payload=loads(row.payload_text, {}),
        status=row.status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        available_at=row.available_at,
        published_at=row.published_at,
        last_error_code=row.last_error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
