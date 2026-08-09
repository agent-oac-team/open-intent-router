import asyncio

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CanonicalTurnModel
from app.repositories.json_utils import dumps, loads
from app.schemas.turns import CanonicalTurn, TurnStatus


class TurnOwnershipConflict(ValueError):
    pass


class TurnTerminalStateError(ValueError):
    pass


class MemoryTurnRepository:
    def __init__(self) -> None:
        self.turns: dict[str, CanonicalTurn] = {}
        self.owner_requests: dict[tuple[str, str, str], str] = {}
        self.request_ids: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create_idempotent(self, turn: CanonicalTurn) -> tuple[CanonicalTurn, bool]:
        async with self._lock:
            key = _owner_request_key(turn)
            existing_id = self.owner_requests.get(key)
            if existing_id is not None:
                return self.turns[existing_id].model_copy(deep=True), False
            if turn.request_id in self.request_ids:
                raise TurnOwnershipConflict("request_id is already owned by another identity")
            existing = self.turns.get(turn.turn_id)
            if existing is not None:
                raise TurnOwnershipConflict("turn_id is already owned by another request")
            stored = turn.model_copy(deep=True)
            self.turns[stored.turn_id] = stored
            self.owner_requests[key] = stored.turn_id
            self.request_ids[stored.request_id] = stored.turn_id
            return stored.model_copy(deep=True), True

    async def get(self, turn_id: str, *, tenant_id: str, user_id: str) -> CanonicalTurn | None:
        turn = self.turns.get(turn_id)
        if turn is None or turn.tenant_id != tenant_id or turn.user_id != user_id:
            return None
        return turn.model_copy(deep=True)

    async def get_by_request(
        self, *, tenant_id: str, user_id: str, request_id: str
    ) -> CanonicalTurn | None:
        turn_id = self.owner_requests.get((tenant_id, user_id, request_id))
        if turn_id is None:
            return None
        return self.turns[turn_id].model_copy(deep=True)

    async def find_by_request_id(self, request_id: str) -> CanonicalTurn | None:
        turn_id = self.request_ids.get(request_id)
        return self.turns[turn_id].model_copy(deep=True) if turn_id else None

    async def get_internal(self, turn_id: str) -> CanonicalTurn | None:
        turn = self.turns.get(turn_id)
        return turn.model_copy(deep=True) if turn else None

    async def update_if_version(
        self, turn: CanonicalTurn, *, expected_version: int
    ) -> CanonicalTurn | None:
        async with self._lock:
            existing = self.turns.get(turn.turn_id)
            if existing is None:
                return None
            if existing.state_version != expected_version:
                return None
            _validate_update(existing, turn, expected_version=expected_version)
            stored = turn.model_copy(deep=True)
            self.turns[stored.turn_id] = stored
            return stored.model_copy(deep=True)


class DatabaseTurnRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def create_idempotent(self, turn: CanonicalTurn) -> tuple[CanonicalTurn, bool]:
        async with self.session_factory() as session:
            session.add(CanonicalTurnModel(**_turn_values(turn)))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self._get_by_request(session, turn)
                if existing is not None:
                    return existing, False
                raise TurnOwnershipConflict("turn_id is already owned by another request") from None
            return turn.model_copy(deep=True), True

    async def get(self, turn_id: str, *, tenant_id: str, user_id: str) -> CanonicalTurn | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(CanonicalTurnModel).where(
                    CanonicalTurnModel.turn_id == turn_id,
                    CanonicalTurnModel.tenant_id == tenant_id,
                    CanonicalTurnModel.user_id == user_id,
                )
            )
            return _turn_from_row(row) if row else None

    async def get_by_request(
        self, *, tenant_id: str, user_id: str, request_id: str
    ) -> CanonicalTurn | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(CanonicalTurnModel).where(
                    CanonicalTurnModel.tenant_id == tenant_id,
                    CanonicalTurnModel.user_id == user_id,
                    CanonicalTurnModel.request_id == request_id,
                )
            )
            return _turn_from_row(row) if row else None

    async def find_by_request_id(self, request_id: str) -> CanonicalTurn | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(CanonicalTurnModel).where(CanonicalTurnModel.request_id == request_id)
            )
            return _turn_from_row(row) if row else None

    async def get_internal(self, turn_id: str) -> CanonicalTurn | None:
        async with self.session_factory() as session:
            row = await session.get(CanonicalTurnModel, turn_id)
            return _turn_from_row(row) if row else None

    async def update_if_version(
        self, turn: CanonicalTurn, *, expected_version: int
    ) -> CanonicalTurn | None:
        async with self.session_factory() as session:
            existing_row = await session.get(CanonicalTurnModel, turn.turn_id)
            if existing_row is None:
                return None
            existing = _turn_from_row(existing_row)
            if existing.state_version != expected_version:
                return None
            _validate_update(existing, turn, expected_version=expected_version)
            result = await session.execute(
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
            if not result.rowcount:
                await session.rollback()
                return None
            await session.commit()
            return turn.model_copy(deep=True)

    async def _get_by_request(
        self, session: AsyncSession, turn: CanonicalTurn
    ) -> CanonicalTurn | None:
        row = await session.scalar(
            select(CanonicalTurnModel).where(
                CanonicalTurnModel.tenant_id == turn.tenant_id,
                CanonicalTurnModel.user_id == turn.user_id,
                CanonicalTurnModel.request_id == turn.request_id,
            )
        )
        return _turn_from_row(row) if row else None


def _validate_update(
    existing: CanonicalTurn, incoming: CanonicalTurn, *, expected_version: int
) -> None:
    identity_fields = ("turn_id", "tenant_id", "user_id", "session_id", "request_id", "source")
    if any(getattr(existing, field) != getattr(incoming, field) for field in identity_fields):
        raise TurnOwnershipConflict("turn identity cannot be changed")
    if existing.status.is_terminal:
        raise TurnTerminalStateError("terminal turn cannot be updated")
    if incoming.state_version != expected_version + 1:
        raise ValueError("turn state_version must advance by exactly one")


def _owner_request_key(turn: CanonicalTurn) -> tuple[str, str, str]:
    return turn.tenant_id, turn.user_id, turn.request_id


def _turn_values(turn: CanonicalTurn) -> dict:
    return {
        "turn_id": turn.turn_id,
        "tenant_id": turn.tenant_id,
        "user_id": turn.user_id,
        "session_id": turn.session_id,
        "request_id": turn.request_id,
        "source": turn.source,
        "status": turn.status.value,
        "state_version": turn.state_version,
        "user_input_text": dumps(turn.user_input.model_dump(mode="json")),
        "references_text": dumps(turn.references.model_dump(mode="json")),
        "final_response_text": (
            dumps(turn.final_response.model_dump(mode="json")) if turn.final_response else None
        ),
        "created_at": turn.created_at,
        "updated_at": turn.updated_at,
        "completed_at": turn.completed_at,
    }


def _turn_from_row(row: CanonicalTurnModel) -> CanonicalTurn:
    return CanonicalTurn.model_validate(
        {
            "turn_id": row.turn_id,
            "tenant_id": row.tenant_id,
            "user_id": row.user_id,
            "session_id": row.session_id,
            "request_id": row.request_id,
            "source": row.source,
            "status": row.status,
            "state_version": row.state_version,
            "user_input": loads(row.user_input_text, {}),
            "references": loads(row.references_text, {}),
            "final_response": loads(row.final_response_text, None),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
        }
    )
