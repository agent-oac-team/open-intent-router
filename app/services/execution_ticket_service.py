import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from app.repositories.execution_tickets import (
    ExecutionTicketStore,
    new_nonce,
    ticket_hash,
)
from app.schemas.delegated_runs import DelegatedRunReference
from app.schemas.execution_tickets import (
    ExecutionTicketClaimResult,
    ExecutionTicketClaims,
    ExecutionTicketIssueResult,
    ExecutionTicketRecord,
    ExecutionTicketStatus,
    LegacyExecutionCorrelationQuery,
    LegacyExecutionCorrelationResult,
)


class ExecutionTicketError(ValueError):
    """Public, non-sensitive Ticket validation failure."""


class ExecutionTicketService:
    def __init__(self, store: ExecutionTicketStore, *, secret: str | None = None) -> None:
        self.store = store
        self.secret = secret
        self._lock = asyncio.Lock()

    async def issue(
        self,
        run: DelegatedRunReference,
        *,
        request_id: str,
        purpose: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> ExecutionTicketIssueResult:
        issued_at = _as_utc(now or datetime.now(UTC))
        if ttl_seconds <= 0:
            raise ExecutionTicketError("Ticket TTL must be positive")
        claims = ExecutionTicketClaims(
            request_id=request_id,
            run_id=run.run_id,
            turn_id=run.turn_id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            agent_id=run.agent_id,
            plan_id=run.plan_id,
            step_id=run.step_id,
            purpose=purpose,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
            nonce=new_nonce(),
        )
        ticket = self._encode(claims)
        await self.store.put(ExecutionTicketRecord(ticket_hash=ticket_hash(ticket), claims=claims))
        return ExecutionTicketIssueResult(ticket=ticket, claims=claims)

    async def resolve_legacy(
        self, query: LegacyExecutionCorrelationQuery
    ) -> LegacyExecutionCorrelationResult:
        matches = await self.store.find_active(query)
        if len(matches) != 1:
            raise ExecutionTicketError("Legacy execution correlation is not unique")
        return LegacyExecutionCorrelationResult(record=matches[0])

    async def claim_legacy(
        self,
        query: LegacyExecutionCorrelationQuery,
        *,
        owner: str,
        lease_seconds: int,
    ) -> ExecutionTicketClaimResult:
        async with self._lock:
            matches = await self.store.find_active(query)
            if len(matches) != 1:
                raise ExecutionTicketError("Legacy execution correlation is not unique")
            if lease_seconds <= 0:
                raise ExecutionTicketError("Ticket lease must be positive")
            current = _as_utc(query.now)
            record = matches[0]
            if (
                record.status == ExecutionTicketStatus.CLAIMED
                and record.lease_expires_at is not None
                and record.lease_expires_at > current
                and record.lease_owner != owner
            ):
                raise ExecutionTicketError("Execution ticket is claimed")
            claimed = record.model_copy(
                update={
                    "status": ExecutionTicketStatus.CLAIMED,
                    "lease_owner": owner,
                    "lease_token": secrets.token_urlsafe(24),
                    "lease_expires_at": current + timedelta(seconds=lease_seconds),
                }
            )
            return ExecutionTicketClaimResult(record=await self.store.update(claimed))

    async def resolve(
        self,
        ticket: str,
        *,
        tenant_id: str,
        user_id: str,
        purpose: str,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        record = await self.store.get(ticket_hash(ticket))
        current = _as_utc(now or datetime.now(UTC))
        if record is None or not _valid_token(ticket, self.secret):
            raise ExecutionTicketError("Invalid execution ticket")
        if record.claims.expires_at <= current:
            if record.status not in {ExecutionTicketStatus.CONSUMED, ExecutionTicketStatus.EXPIRED}:
                await self.store.update(
                    record.model_copy(update={"status": ExecutionTicketStatus.EXPIRED})
                )
            raise ExecutionTicketError("Execution ticket expired")
        if (
            record.status in {ExecutionTicketStatus.EXPIRED, ExecutionTicketStatus.REVOKED}
            or record.claims.tenant_id != tenant_id
            or record.claims.user_id != user_id
            or record.claims.purpose != purpose
        ):
            raise ExecutionTicketError("Invalid execution ticket")
        return record

    async def claim(
        self,
        ticket: str,
        *,
        tenant_id: str,
        user_id: str,
        purpose: str,
        owner: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> ExecutionTicketClaimResult:
        async with self._lock:
            current = _as_utc(now or datetime.now(UTC))
            record = await self.resolve(
                ticket,
                tenant_id=tenant_id,
                user_id=user_id,
                purpose=purpose,
                now=current,
            )
            if record.status == ExecutionTicketStatus.CONSUMED:
                return ExecutionTicketClaimResult(record=record, duplicate=True)
            if lease_seconds <= 0:
                raise ExecutionTicketError("Ticket lease must be positive")
            if (
                record.status == ExecutionTicketStatus.CLAIMED
                and record.lease_expires_at is not None
                and record.lease_expires_at > current
                and record.lease_owner != owner
            ):
                raise ExecutionTicketError("Execution ticket is claimed")
            claimed = record.model_copy(
                update={
                    "status": ExecutionTicketStatus.CLAIMED,
                    "lease_owner": owner,
                    "lease_token": secrets.token_urlsafe(24),
                    "lease_expires_at": current + timedelta(seconds=lease_seconds),
                }
            )
            return ExecutionTicketClaimResult(record=await self.store.update(claimed))

    async def consume(
        self,
        ticket: str,
        *,
        event_id: str,
        owner: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        async with self._lock:
            record = await self.store.get(ticket_hash(ticket))
            current = _as_utc(now or datetime.now(UTC))
            if record is None:
                raise ExecutionTicketError("Invalid execution ticket")
            if record.status == ExecutionTicketStatus.CONSUMED:
                if record.consumed_event_id == event_id:
                    return record
                raise ExecutionTicketError("Execution ticket already consumed")
            if (
                record.status != ExecutionTicketStatus.CLAIMED
                or record.lease_owner != owner
                or record.lease_token != lease_token
                or record.lease_expires_at is None
                or record.lease_expires_at <= current
            ):
                raise ExecutionTicketError("Execution ticket claim is invalid")
            return await self.store.update(
                record.model_copy(
                    update={
                        "status": ExecutionTicketStatus.CONSUMED,
                        "consumed_event_id": event_id,
                        "consumed_at": current,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                    }
                )
            )

    async def release_after_progress(
        self,
        ticket: str,
        *,
        owner: str,
        lease_token: str,
        run_state_version: int,
        event_sequence: int,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        async with self._lock:
            record = await self.store.get(ticket_hash(ticket))
            current = _as_utc(now or datetime.now(UTC))
            if (
                record is None
                or record.status != ExecutionTicketStatus.CLAIMED
                or record.lease_owner != owner
                or record.lease_token != lease_token
                or record.lease_expires_at is None
                or record.lease_expires_at <= current
                or run_state_version <= record.run_state_version
                or event_sequence <= record.event_sequence
            ):
                raise ExecutionTicketError("Execution ticket claim is invalid")
            return await self.store.update(
                record.model_copy(
                    update={
                        "status": ExecutionTicketStatus.ISSUED,
                        "run_state_version": run_state_version,
                        "event_sequence": event_sequence,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                    }
                )
            )

    async def release_legacy_after_progress(
        self,
        ticket_hash_value: str,
        *,
        owner: str,
        lease_token: str,
        run_state_version: int,
        event_sequence: int,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        return await self._release_record_after_progress(
            ticket_hash_value,
            owner=owner,
            lease_token=lease_token,
            run_state_version=run_state_version,
            event_sequence=event_sequence,
            now=now,
        )

    async def consume_legacy(
        self,
        ticket_hash_value: str,
        *,
        event_id: str,
        owner: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        return await self._consume_record(
            ticket_hash_value,
            event_id=event_id,
            owner=owner,
            lease_token=lease_token,
            now=now,
        )

    async def _release_record_after_progress(
        self,
        ticket_hash_value: str,
        *,
        owner: str,
        lease_token: str,
        run_state_version: int,
        event_sequence: int,
        now: datetime | None,
    ) -> ExecutionTicketRecord:
        async with self._lock:
            record = await self.store.get(ticket_hash_value)
            current = _as_utc(now or datetime.now(UTC))
            if (
                record is None
                or record.status != ExecutionTicketStatus.CLAIMED
                or record.lease_owner != owner
                or record.lease_token != lease_token
                or record.lease_expires_at is None
                or record.lease_expires_at <= current
                or run_state_version <= record.run_state_version
                or event_sequence <= record.event_sequence
            ):
                raise ExecutionTicketError("Execution ticket claim is invalid")
            return await self.store.update(
                record.model_copy(
                    update={
                        "status": ExecutionTicketStatus.ISSUED,
                        "run_state_version": run_state_version,
                        "event_sequence": event_sequence,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                    }
                )
            )

    async def _consume_record(
        self,
        ticket_hash_value: str,
        *,
        event_id: str,
        owner: str,
        lease_token: str,
        now: datetime | None,
    ) -> ExecutionTicketRecord:
        async with self._lock:
            record = await self.store.get(ticket_hash_value)
            current = _as_utc(now or datetime.now(UTC))
            if record is None:
                raise ExecutionTicketError("Invalid execution ticket")
            if record.status == ExecutionTicketStatus.CONSUMED:
                if record.consumed_event_id == event_id:
                    return record
                raise ExecutionTicketError("Execution ticket already consumed")
            if (
                record.status != ExecutionTicketStatus.CLAIMED
                or record.lease_owner != owner
                or record.lease_token != lease_token
                or record.lease_expires_at is None
                or record.lease_expires_at <= current
            ):
                raise ExecutionTicketError("Execution ticket claim is invalid")
            return await self.store.update(
                record.model_copy(
                    update={
                        "status": ExecutionTicketStatus.CONSUMED,
                        "consumed_event_id": event_id,
                        "consumed_at": current,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                    }
                )
            )

    def _encode(self, claims: ExecutionTicketClaims) -> str:
        raw = secrets.token_urlsafe(32)
        if self.secret:
            digest = hashlib.sha256(f"{raw}.{self.secret}".encode()).hexdigest()[:32]
            return f"{raw}.{digest}"
        return raw


def _valid_token(ticket: str, secret: str | None) -> bool:
    if not ticket or len(ticket) > 512 or any(char.isspace() for char in ticket):
        return False
    if secret is None:
        return True
    raw, separator, signature = ticket.rpartition(".")
    if not separator or not raw or not signature:
        return False
    expected = hashlib.sha256(f"{raw}.{secret}".encode()).hexdigest()[:32]
    return secrets.compare_digest(signature, expected)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
