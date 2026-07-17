import asyncio

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CanonicalTurnModel, TurnOutboxModel
from app.repositories.turn_outbox import _event_values
from app.repositories.turns import _turn_from_row, _turn_values, _validate_update
from app.schemas.turns import CanonicalTurn, TurnOutboxEvent, TurnStatus


class MemoryRouteTurnCompletionStore:
    def __init__(self, *, turn_repository, outbox_repository) -> None:
        self.turn_repository = turn_repository
        self.outbox_repository = outbox_repository
        self._lock = asyncio.Lock()

    async def complete(
        self,
        turn: CanonicalTurn,
        *,
        expected_version: int,
        outbox: TurnOutboxEvent,
    ) -> CanonicalTurn | None:
        async with self._lock:
            stored = await self.turn_repository.update_if_version(
                turn, expected_version=expected_version
            )
            if stored is None:
                return None
            await self.outbox_repository.add_idempotent(outbox)
            return stored


class DatabaseRouteTurnCompletionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def complete(
        self,
        turn: CanonicalTurn,
        *,
        expected_version: int,
        outbox: TurnOutboxEvent,
    ) -> CanonicalTurn | None:
        async with self.session_factory() as session:
            row = await session.get(CanonicalTurnModel, turn.turn_id)
            if row is None:
                return None
            existing = _turn_from_row(row)
            if existing.state_version != expected_version:
                return None
            _validate_update(existing, turn, expected_version=expected_version)
            updated = await session.execute(
                update(CanonicalTurnModel)
                .where(
                    CanonicalTurnModel.turn_id == turn.turn_id,
                    CanonicalTurnModel.tenant_id == turn.tenant_id,
                    CanonicalTurnModel.user_id == turn.user_id,
                    CanonicalTurnModel.state_version == expected_version,
                    CanonicalTurnModel.status.not_in(
                        [status.value for status in TurnStatus if status.is_terminal]
                    ),
                )
                .values(**_turn_values(turn))
            )
            if updated.rowcount != 1:
                await session.rollback()
                return None
            session.add(TurnOutboxModel(**_event_values(outbox)))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return turn.model_copy(deep=True)
