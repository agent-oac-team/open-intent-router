"""Durable, Host-neutral idempotency records for External Executor acceptance."""

import asyncio
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ExternalExecutionAcceptanceModel
from app.schemas.external_execution import ExternalExecutionAcceptanceReservation


class ExternalExecutionAcceptanceConflict(ValueError):
    """An acceptance ID was reused for a different trusted request."""


class ExternalExecutionAcceptanceStore(Protocol):
    async def record_accepted(
        self, reservation: ExternalExecutionAcceptanceReservation
    ) -> bool: ...


class MemoryExternalExecutionAcceptanceStore:
    def __init__(self) -> None:
        self.records: dict[str, ExternalExecutionAcceptanceReservation] = {}
        self._lock = asyncio.Lock()

    async def record_accepted(self, reservation: ExternalExecutionAcceptanceReservation) -> bool:
        async with self._lock:
            existing = self.records.get(reservation.acceptance_id)
            if existing is None:
                self.records[reservation.acceptance_id] = reservation
                return True
            _validate_same_reservation(existing, reservation)
            return False


class DatabaseExternalExecutionAcceptanceStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def record_accepted(self, reservation: ExternalExecutionAcceptanceReservation) -> bool:
        try:
            async with self.session_factory() as session, session.begin():
                session.add(
                    ExternalExecutionAcceptanceModel(
                        acceptance_id=reservation.acceptance_id,
                        request_fingerprint=reservation.request_fingerprint,
                        executor_ref=reservation.executor_ref,
                    )
                )
                await session.flush()
                return True
        except IntegrityError:
            async with self.session_factory() as session:
                existing = await session.get(
                    ExternalExecutionAcceptanceModel,
                    reservation.acceptance_id,
                )
            if existing is None:
                raise
            _validate_same_reservation(
                ExternalExecutionAcceptanceReservation(
                    acceptance_id=existing.acceptance_id,
                    request_fingerprint=existing.request_fingerprint,
                    executor_ref=existing.executor_ref,
                ),
                reservation,
            )
            return False


def _validate_same_reservation(
    existing: ExternalExecutionAcceptanceReservation,
    candidate: ExternalExecutionAcceptanceReservation,
) -> None:
    if (
        existing.request_fingerprint != candidate.request_fingerprint
        or existing.executor_ref != candidate.executor_ref
    ):
        raise ExternalExecutionAcceptanceConflict("External Execution acceptance conflicts")
