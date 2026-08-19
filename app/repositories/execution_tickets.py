import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentRunModel, ExecutionTicketModel
from app.repositories.json_utils import dumps, loads
from app.schemas.execution_tickets import (
    ExecutionTicketClaims,
    ExecutionTicketRecord,
    ExecutionTicketStatus,
    LegacyExecutionCorrelationQuery,
)


class ExecutionTicketConflict(ValueError):
    pass


class ExecutionTicketIssuanceUnavailable(ExecutionTicketConflict):
    """A canonical External Execution Run has a durable failed issuance fence."""


class _RetryCanonicalTicketIssue(RuntimeError):
    """A competing transaction owns the per-Run issuance fence."""


class ExecutionTicketStore(Protocol):
    async def put(self, record: ExecutionTicketRecord) -> None: ...

    async def issue_or_reuse_active_for_run(
        self,
        record: ExecutionTicketRecord,
        *,
        now: datetime,
    ) -> ExecutionTicketRecord: ...

    async def recover_active_or_mark_issue_failed(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None: ...

    async def find_canonical_active_for_run(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None: ...

    async def get(self, ticket_hash: str) -> ExecutionTicketRecord | None: ...

    async def claim(
        self,
        ticket_hash: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionTicketRecord | None: ...

    async def finalize_claim(
        self,
        record: ExecutionTicketRecord,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None: ...

    async def expire(self, ticket_hash: str) -> ExecutionTicketRecord | None: ...

    async def find_active(
        self, query: LegacyExecutionCorrelationQuery
    ) -> list[ExecutionTicketRecord]: ...


class MemoryExecutionTicketStore:
    def __init__(self) -> None:
        self.records: dict[str, ExecutionTicketRecord] = {}
        self._canonical_issue_states: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: ExecutionTicketRecord) -> None:
        async with self._lock:
            if record.ticket_hash in self.records:
                raise ExecutionTicketConflict("Execution Ticket already exists")
            self.records[record.ticket_hash] = record

    async def issue_or_reuse_active_for_run(
        self,
        record: ExecutionTicketRecord,
        *,
        now: datetime,
    ) -> ExecutionTicketRecord:
        """Atomically return the one usable Ticket for a Run/purpose pair.

        A Route retry must return the same opaque bearer rather than invalidate a
        first successful response. We retain the existing active record and only
        refresh its canonical callback cursor when no callback owns the lease.
        """

        async with self._lock:
            if not record.canonical_reuse:
                raise ExecutionTicketConflict("Canonical Ticket reuse is required")
            if record.ticket_hash in self.records:
                raise ExecutionTicketConflict("Execution Ticket already exists")
            key = _canonical_ticket_key(record.claims.run_id, record.claims.purpose)
            if self._canonical_issue_states.get(key) == "failed":
                raise ExecutionTicketIssuanceUnavailable("Execution Ticket issuance is unavailable")
            active = [
                (ticket_hash_value, current)
                for ticket_hash_value, current in self.records.items()
                if _is_active_for_same_run(current, record)
            ]
            if len(active) > 1:
                raise ExecutionTicketConflict("Execution Ticket active mapping is not unique")
            if active:
                ticket_hash_value, current = active[0]
                if current.claims.expires_at <= now:
                    self.records[ticket_hash_value] = _expired(current)
                else:
                    reusable = _refresh_active_cursor(current, record, now=now)
                    self.records[ticket_hash_value] = reusable
                    self._canonical_issue_states[key] = "issued"
                    return reusable.model_copy(deep=True)
            self.records[record.ticket_hash] = record
            self._canonical_issue_states[key] = "issued"
            return record.model_copy(deep=True)

    async def recover_active_or_mark_issue_failed(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        """Return an unknown-commit Ticket, or durably fence this Run from issue.

        The memory implementation has one lock for the Ticket store, which is
        its equivalent serialization point for a Run.  A later retry cannot
        issue after this method chooses the safe failure branch.
        """

        async with self._lock:
            key = _canonical_ticket_key(run_id, purpose)
            active = [
                (ticket_hash_value, current)
                for ticket_hash_value, current in self.records.items()
                if _is_canonical_active_for_run(current, run_id=run_id, purpose=purpose)
            ]
            if len(active) > 1:
                raise ExecutionTicketConflict("Execution Ticket active mapping is not unique")
            if active:
                ticket_hash_value, current = active[0]
                if current.claims.expires_at > now:
                    self._canonical_issue_states[key] = "issued"
                    return current.model_copy(deep=True)
                self.records[ticket_hash_value] = _expired(current)
            self._canonical_issue_states[key] = "failed"
            return None

    async def find_canonical_active_for_run(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        async with self._lock:
            active = [
                (ticket_hash_value, current)
                for ticket_hash_value, current in self.records.items()
                if _is_canonical_active_for_run(current, run_id=run_id, purpose=purpose)
            ]
            if len(active) > 1:
                raise ExecutionTicketConflict("Execution Ticket active mapping is not unique")
            if not active:
                return None
            ticket_hash_value, current = active[0]
            if current.claims.expires_at <= now:
                self.records[ticket_hash_value] = _expired(current)
                return None
            return current.model_copy(deep=True)

    async def get(self, ticket_hash: str) -> ExecutionTicketRecord | None:
        async with self._lock:
            record = self.records.get(ticket_hash)
            return record.model_copy(deep=True) if record else None

    async def claim(
        self,
        ticket_hash: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionTicketRecord | None:
        async with self._lock:
            record = self.records.get(ticket_hash)
            if record is None or not _claimable(record, now):
                return None
            claimed = record.model_copy(
                update={
                    "status": ExecutionTicketStatus.CLAIMED,
                    "lease_owner": owner,
                    "lease_token": lease_token,
                    "lease_expires_at": lease_expires_at,
                }
            )
            self.records[ticket_hash] = claimed
            return claimed.model_copy(deep=True)

    async def finalize_claim(
        self,
        record: ExecutionTicketRecord,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        async with self._lock:
            current = self.records.get(record.ticket_hash)
            if current is None or not _claim_owned(current, owner, lease_token, now):
                return None
            self.records[record.ticket_hash] = record
            return record.model_copy(deep=True)

    async def expire(self, ticket_hash: str) -> ExecutionTicketRecord | None:
        async with self._lock:
            record = self.records.get(ticket_hash)
            if record is None or record.status not in {
                ExecutionTicketStatus.ISSUED,
                ExecutionTicketStatus.CLAIMED,
            }:
                return None
            expired = record.model_copy(
                update={
                    "status": ExecutionTicketStatus.EXPIRED,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                }
            )
            self.records[ticket_hash] = expired
            return expired.model_copy(deep=True)

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
            session.add(ExecutionTicketModel(**_ticket_model_values(record)))

    async def issue_or_reuse_active_for_run(
        self,
        record: ExecutionTicketRecord,
        *,
        now: datetime,
    ) -> ExecutionTicketRecord:
        """Return the one canonical Ticket using a durable per-Run fence.

        PostgreSQL row locks provide the primary serialization point.  The
        conditional state transition and scoped partial unique index are the
        backstop for engines such as SQLite that do not honour ``FOR UPDATE``.
        The index is deliberately scoped to canonical External Execution
        Tickets, preserving frozen legacy ambiguity behaviour for ordinary
        Ticket issuance.
        """

        if not record.canonical_reuse:
            raise ExecutionTicketConflict("Canonical Ticket reuse is required")
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                return await self._issue_or_reuse_once(record, now=now)
            except _RetryCanonicalTicketIssue as error:
                last_error = error
            except IntegrityError as error:
                reusable = await self._active_canonical_record(
                    run_id=record.claims.run_id,
                    purpose=record.claims.purpose,
                    now=now,
                )
                if reusable is not None:
                    return reusable
                last_error = error
            except OperationalError as error:
                # SQLite can report a transient writer lock while another
                # worker commits the same per-Run fence.
                last_error = error
            if attempt < 4:
                await asyncio.sleep(0.01 * (attempt + 1))
        if last_error is not None:
            raise last_error
        raise ExecutionTicketConflict("Execution Ticket issuance did not converge")

    async def recover_active_or_mark_issue_failed(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        """Resolve an unknown commit, otherwise atomically fence safe failure."""

        last_error: Exception | None = None
        for attempt in range(5):
            try:
                return await self._recover_or_mark_issue_failed_once(
                    run_id=run_id,
                    purpose=purpose,
                    now=now,
                )
            except _RetryCanonicalTicketIssue as error:
                last_error = error
            except OperationalError as error:
                last_error = error
            if attempt < 4:
                await asyncio.sleep(0.01 * (attempt + 1))
        if last_error is not None:
            raise last_error
        raise ExecutionTicketConflict("Execution Ticket issuance recovery did not converge")

    async def find_canonical_active_for_run(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        return await self._active_canonical_record(
            run_id=run_id,
            purpose=purpose,
            now=now,
        )

    async def _issue_or_reuse_once(
        self,
        record: ExecutionTicketRecord,
        *,
        now: datetime,
    ) -> ExecutionTicketRecord:
        async with self.session_factory() as session, session.begin():
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == record.claims.run_id)
                .with_for_update()
            )
            _validate_active_run(run)
            if await session.get(ExecutionTicketModel, record.ticket_hash):
                raise ExecutionTicketConflict("Execution Ticket already exists")
            active = await _active_canonical_rows(
                session,
                run_id=record.claims.run_id,
                purpose=record.claims.purpose,
                lock=True,
            )
            reusable = _reuse_or_expire_active_rows(active, record=record, now=now)
            if reusable is not None:
                run.external_ticket_issuance_state = "issued"
                return reusable
            await session.flush()
            if run.external_ticket_issuance_state == "failed":
                raise ExecutionTicketIssuanceUnavailable("Execution Ticket issuance is unavailable")
            acquired = await session.execute(
                update(AgentRunModel)
                .where(
                    AgentRunModel.run_id == record.claims.run_id,
                    AgentRunModel.status.in_(_ACTIVE_RUN_STATUSES),
                    or_(
                        AgentRunModel.external_ticket_issuance_state.is_(None),
                        AgentRunModel.external_ticket_issuance_state.in_(("unissued", "issued")),
                    ),
                )
                .values(external_ticket_issuance_state="issuing")
            )
            if acquired.rowcount != 1:
                raise _RetryCanonicalTicketIssue()
            session.add(ExecutionTicketModel(**_ticket_model_values(record)))
            run.external_ticket_issuance_state = "issued"
            return record

    async def _recover_or_mark_issue_failed_once(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        async with self.session_factory() as session, session.begin():
            run = await session.scalar(
                select(AgentRunModel).where(AgentRunModel.run_id == run_id).with_for_update()
            )
            if run is None:
                raise ExecutionTicketConflict("Delegated Run not found")
            active = await _active_canonical_rows(
                session,
                run_id=run_id,
                purpose=purpose,
                lock=True,
            )
            reusable = _reuse_or_expire_active_rows(active, record=None, now=now)
            if reusable is not None:
                run.external_ticket_issuance_state = "issued"
                return reusable
            await session.flush()
            if run.external_ticket_issuance_state == "failed":
                return None
            if run.external_ticket_issuance_state == "issuing":
                raise _RetryCanonicalTicketIssue()
            failed = await session.execute(
                update(AgentRunModel)
                .where(
                    AgentRunModel.run_id == run_id,
                    AgentRunModel.status.in_(_ACTIVE_RUN_STATUSES),
                    or_(
                        AgentRunModel.external_ticket_issuance_state.is_(None),
                        AgentRunModel.external_ticket_issuance_state.in_(("unissued", "issued")),
                    ),
                )
                .values(external_ticket_issuance_state="failed")
            )
            if failed.rowcount != 1:
                raise _RetryCanonicalTicketIssue()
            return None

    async def _active_canonical_record(
        self,
        *,
        run_id: str,
        purpose: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        async with self.session_factory() as session:
            active = await _active_canonical_rows(
                session,
                run_id=run_id,
                purpose=purpose,
                lock=False,
            )
            reusable = _reuse_or_expire_active_rows(active, record=None, now=now)
            return reusable

    async def get(self, ticket_hash: str) -> ExecutionTicketRecord | None:
        async with self.session_factory() as session:
            row = await session.get(ExecutionTicketModel, ticket_hash)
            return _record_from_row(row) if row else None

    async def claim(
        self,
        ticket_hash: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ExecutionTicketRecord | None:
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                update(ExecutionTicketModel)
                .where(
                    ExecutionTicketModel.ticket_hash == ticket_hash,
                    or_(
                        ExecutionTicketModel.status == ExecutionTicketStatus.ISSUED.value,
                        and_(
                            ExecutionTicketModel.status == ExecutionTicketStatus.CLAIMED.value,
                            ExecutionTicketModel.lease_expires_at.is_not(None),
                            ExecutionTicketModel.lease_expires_at <= now,
                        ),
                    ),
                )
                .values(
                    status=ExecutionTicketStatus.CLAIMED.value,
                    lease_owner=owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                )
            )
            if result.rowcount != 1:
                return None
            row = await session.get(ExecutionTicketModel, ticket_hash)
            if row is None:
                raise ExecutionTicketConflict("Execution Ticket not found")
            return _record_from_row(row)

    async def finalize_claim(
        self,
        record: ExecutionTicketRecord,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
    ) -> ExecutionTicketRecord | None:
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                update(ExecutionTicketModel)
                .where(
                    ExecutionTicketModel.ticket_hash == record.ticket_hash,
                    ExecutionTicketModel.status == ExecutionTicketStatus.CLAIMED.value,
                    ExecutionTicketModel.lease_owner == owner,
                    ExecutionTicketModel.lease_token == lease_token,
                    ExecutionTicketModel.lease_expires_at.is_not(None),
                    ExecutionTicketModel.lease_expires_at > now,
                )
                .values(
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
            if result.rowcount != 1:
                return None
            row = await session.get(ExecutionTicketModel, record.ticket_hash)
            if row is None:
                raise ExecutionTicketConflict("Execution Ticket not found")
            return _record_from_row(row)

    async def expire(self, ticket_hash: str) -> ExecutionTicketRecord | None:
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                update(ExecutionTicketModel)
                .where(
                    ExecutionTicketModel.ticket_hash == ticket_hash,
                    ExecutionTicketModel.status.in_(
                        (
                            ExecutionTicketStatus.ISSUED.value,
                            ExecutionTicketStatus.CLAIMED.value,
                        )
                    ),
                )
                .values(
                    status=ExecutionTicketStatus.EXPIRED.value,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            if result.rowcount != 1:
                return None
            row = await session.get(ExecutionTicketModel, ticket_hash)
            if row is None:
                raise ExecutionTicketConflict("Execution Ticket not found")
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


def _expired(record: ExecutionTicketRecord) -> ExecutionTicketRecord:
    return record.model_copy(
        update={
            "status": ExecutionTicketStatus.EXPIRED,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
        }
    )


_ACTIVE_RUN_STATUSES = ("pending", "running", "blocked")


def _canonical_ticket_key(run_id: str, purpose: str) -> tuple[str, str]:
    return run_id, purpose


def _ticket_model_values(record: ExecutionTicketRecord) -> dict[str, object]:
    return {
        "ticket_hash": record.ticket_hash,
        "request_id": record.claims.request_id,
        "run_id": record.claims.run_id,
        "turn_id": record.claims.turn_id,
        "tenant_id": record.claims.tenant_id,
        "user_id": record.claims.user_id,
        "agent_id": record.claims.agent_id,
        "plan_id": record.claims.plan_id,
        "step_id": record.claims.step_id,
        "purpose": record.claims.purpose,
        "claims_text": dumps(record.claims.model_dump(mode="json")),
        "status": record.status.value,
        "run_state_version": record.run_state_version,
        "event_sequence": record.event_sequence,
        "lease_owner": record.lease_owner,
        "lease_token": record.lease_token,
        "lease_expires_at": record.lease_expires_at,
        "consumed_event_id": record.consumed_event_id,
        "consumed_at": record.consumed_at,
        "canonical_reuse": record.canonical_reuse,
    }


def _validate_active_run(run: AgentRunModel | None) -> None:
    if run is None:
        raise ExecutionTicketConflict("Delegated Run not found")
    if run.status not in _ACTIVE_RUN_STATUSES:
        raise ExecutionTicketIssuanceUnavailable("Delegated Run is not active")


async def _active_canonical_rows(
    session: AsyncSession,
    *,
    run_id: str,
    purpose: str,
    lock: bool,
) -> list[ExecutionTicketModel]:
    statement = select(ExecutionTicketModel).where(
        ExecutionTicketModel.run_id == run_id,
        ExecutionTicketModel.purpose == purpose,
        ExecutionTicketModel.canonical_reuse.is_(True),
        ExecutionTicketModel.status.in_(
            (
                ExecutionTicketStatus.ISSUED.value,
                ExecutionTicketStatus.CLAIMED.value,
            )
        ),
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalars().all()


def _reuse_or_expire_active_rows(
    rows: list[ExecutionTicketModel],
    *,
    record: ExecutionTicketRecord | None,
    now: datetime,
) -> ExecutionTicketRecord | None:
    reusable: list[tuple[ExecutionTicketModel, ExecutionTicketRecord]] = []
    for row in rows:
        current = _record_from_row(row)
        if current.claims.expires_at <= now:
            _apply_reusable_record(row, _expired(current))
            continue
        reusable.append((row, current))
    if len(reusable) > 1:
        raise ExecutionTicketConflict("Execution Ticket active mapping is not unique")
    if not reusable:
        return None
    row, current = reusable[0]
    result = _refresh_active_cursor(current, record, now=now) if record else current
    _apply_reusable_record(row, result)
    return result


def _apply_reusable_record(row: ExecutionTicketModel, record: ExecutionTicketRecord) -> None:
    row.status = record.status.value
    row.run_state_version = record.run_state_version
    row.event_sequence = record.event_sequence
    row.lease_owner = record.lease_owner
    row.lease_token = record.lease_token
    row.lease_expires_at = record.lease_expires_at
    row.consumed_event_id = record.consumed_event_id
    row.consumed_at = record.consumed_at


def _refresh_active_cursor(
    current: ExecutionTicketRecord,
    candidate: ExecutionTicketRecord,
    *,
    now: datetime,
) -> ExecutionTicketRecord:
    """Advance a reusable Ticket without disturbing a live callback lease."""

    if (
        current.status == ExecutionTicketStatus.CLAIMED
        and current.lease_expires_at is not None
        and current.lease_expires_at > now
    ):
        return current
    updates: dict[str, object] = {
        "run_state_version": max(current.run_state_version, candidate.run_state_version),
        "event_sequence": max(current.event_sequence, candidate.event_sequence),
    }
    if current.status == ExecutionTicketStatus.CLAIMED:
        updates.update(
            {
                "status": ExecutionTicketStatus.ISSUED,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
            }
        )
    return current.model_copy(update=updates)


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
        canonical_reuse=row.canonical_reuse,
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


def _is_active_for_same_run(
    existing: ExecutionTicketRecord,
    replacement: ExecutionTicketRecord,
) -> bool:
    return (
        existing.canonical_reuse
        and replacement.canonical_reuse
        and existing.claims.run_id == replacement.claims.run_id
        and existing.claims.purpose == replacement.claims.purpose
        and existing.status in {ExecutionTicketStatus.ISSUED, ExecutionTicketStatus.CLAIMED}
    )


def _is_canonical_active_for_run(
    record: ExecutionTicketRecord,
    *,
    run_id: str,
    purpose: str,
) -> bool:
    return (
        record.canonical_reuse
        and record.claims.run_id == run_id
        and record.claims.purpose == purpose
        and record.status in {ExecutionTicketStatus.ISSUED, ExecutionTicketStatus.CLAIMED}
    )


def _claimable(record: ExecutionTicketRecord, now: datetime) -> bool:
    return record.status == ExecutionTicketStatus.ISSUED or (
        record.status == ExecutionTicketStatus.CLAIMED
        and record.lease_expires_at is not None
        and record.lease_expires_at <= _as_utc(now)
    )


def _claim_owned(
    record: ExecutionTicketRecord,
    owner: str,
    lease_token: str,
    now: datetime,
) -> bool:
    return (
        record.status == ExecutionTicketStatus.CLAIMED
        and record.lease_owner == owner
        and record.lease_token == lease_token
        and record.lease_expires_at is not None
        and record.lease_expires_at > _as_utc(now)
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
