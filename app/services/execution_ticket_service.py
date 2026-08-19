import hashlib
import hmac
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

    async def issue(
        self,
        run: DelegatedRunReference,
        *,
        request_id: str,
        purpose: str,
        ttl_seconds: int,
        now: datetime | None = None,
        reuse_active_for_run: bool = False,
    ) -> ExecutionTicketIssueResult:
        issued_at = _as_utc(now or datetime.now(UTC))
        if ttl_seconds <= 0:
            raise ExecutionTicketError("Ticket TTL must be positive")
        if reuse_active_for_run and not self.secret:
            raise ExecutionTicketError("Ticket signing secret is required for retry-safe reuse")
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
        record = ExecutionTicketRecord(
            ticket_hash=ticket_hash(ticket),
            claims=claims,
            run_state_version=run.state_version,
            event_sequence=run.event_sequence,
            canonical_reuse=reuse_active_for_run,
        )
        if reuse_active_for_run:
            record = await self.store.issue_or_reuse_active_for_run(record, now=issued_at)
            ticket = self._encode(record.claims)
        else:
            await self.store.put(record)
        return ExecutionTicketIssueResult(ticket=ticket, claims=record.claims)

    async def recover_after_issue_failure(
        self,
        run: DelegatedRunReference,
        *,
        purpose: str,
        now: datetime | None = None,
    ) -> ExecutionTicketIssueResult | None:
        """Recover a committed canonical Ticket before safe Run compensation.

        A network/process failure can occur after the Ticket transaction commits.
        The store therefore owns a durable issuance fence: it either returns the
        one active bearer or records that this Run can no longer receive one.
        """

        recovered = await self.recover_committed_ticket(
            run,
            purpose=purpose,
            now=now,
        )
        if recovered is not None:
            return recovered
        if not self.secret:
            raise ExecutionTicketError("Ticket signing secret is required for retry-safe reuse")
        record = await self.store.recover_active_or_mark_issue_failed(
            run_id=run.run_id,
            purpose=purpose,
            now=_as_utc(now or datetime.now(UTC)),
        )
        if record is None:
            return None
        return ExecutionTicketIssueResult(
            ticket=self._encode(record.claims),
            claims=record.claims,
        )

    async def recover_committed_ticket(
        self,
        run: DelegatedRunReference,
        *,
        purpose: str,
        now: datetime | None = None,
    ) -> ExecutionTicketIssueResult | None:
        """Return a Ticket that committed before the caller lost its response.

        This is deliberately read/recovery-only: a retry of an already-created
        Run must remain eligible for a later handoff if no Ticket was committed.
        Only the creator's compensating path may install the failed issuance
        fence through :meth:`recover_after_issue_failure`.
        """

        if not self.secret:
            raise ExecutionTicketError("Ticket signing secret is required for retry-safe reuse")
        record = await self.store.find_canonical_active_for_run(
            run_id=run.run_id,
            purpose=purpose,
            now=_as_utc(now or datetime.now(UTC)),
        )
        if record is None:
            return None
        return ExecutionTicketIssueResult(
            ticket=self._encode(record.claims),
            claims=record.claims,
        )

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
        if lease_seconds <= 0:
            raise ExecutionTicketError("Ticket lease must be positive")
        matches = await self.store.find_active(query)
        if len(matches) != 1:
            raise ExecutionTicketError("Legacy execution correlation is not unique")
        current = _as_utc(query.now)
        claimed = await self.store.claim(
            matches[0].ticket_hash,
            owner=owner,
            lease_token=secrets.token_urlsafe(24),
            now=current,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
        )
        if claimed is None:
            raise ExecutionTicketError("Execution ticket is claimed")
        return ExecutionTicketClaimResult(record=claimed)

    async def resolve(
        self,
        ticket: str,
        *,
        tenant_id: str,
        user_id: str,
        purpose: str,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        record = await self.resolve_bound(ticket, purpose=purpose, now=now)
        if record.claims.tenant_id != tenant_id or record.claims.user_id != user_id:
            raise ExecutionTicketError("Invalid execution ticket")
        return record

    async def resolve_bound(
        self,
        ticket: str,
        *,
        purpose: str,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        """Resolve a bearer Ticket before deriving its bound execution identity."""
        record = await self.store.get(ticket_hash(ticket))
        current = _as_utc(now or datetime.now(UTC))
        if record is None or not _valid_token(ticket, self.secret):
            raise ExecutionTicketError("Invalid execution ticket")
        if record.claims.expires_at <= current:
            if record.status not in {ExecutionTicketStatus.CONSUMED, ExecutionTicketStatus.EXPIRED}:
                await self.store.expire(record.ticket_hash)
            raise ExecutionTicketError("Execution ticket expired")
        if (
            record.status in {ExecutionTicketStatus.EXPIRED, ExecutionTicketStatus.REVOKED}
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
        if lease_seconds <= 0:
            raise ExecutionTicketError("Ticket lease must be positive")
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
        claimed = await self.store.claim(
            record.ticket_hash,
            owner=owner,
            lease_token=secrets.token_urlsafe(24),
            now=current,
            lease_expires_at=current + timedelta(seconds=lease_seconds),
        )
        if claimed is not None:
            return ExecutionTicketClaimResult(record=claimed)
        canonical = await self.store.get(record.ticket_hash)
        if canonical is not None and canonical.status == ExecutionTicketStatus.CONSUMED:
            return ExecutionTicketClaimResult(record=canonical, duplicate=True)
        raise ExecutionTicketError("Execution ticket is claimed")

    async def release_claim(
        self,
        ticket: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        """Release a claim after an idempotent command observes an existing Event."""
        return await self._finalize_claim(
            await self.store.get(ticket_hash(ticket)),
            owner=owner,
            lease_token=lease_token,
            current=_as_utc(now or datetime.now(UTC)),
            updates={
                "status": ExecutionTicketStatus.ISSUED,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
            },
        )

    async def consume(
        self,
        ticket: str,
        *,
        event_id: str,
        owner: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> ExecutionTicketRecord:
        return await self._consume_record(
            ticket_hash(ticket),
            event_id=event_id,
            owner=owner,
            lease_token=lease_token,
            now=now,
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
        return await self._release_record_after_progress(
            ticket_hash(ticket),
            owner=owner,
            lease_token=lease_token,
            run_state_version=run_state_version,
            event_sequence=event_sequence,
            now=now,
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
        record = await self.store.get(ticket_hash_value)
        if (
            record is None
            or run_state_version <= record.run_state_version
            or event_sequence <= record.event_sequence
        ):
            raise ExecutionTicketError("Execution ticket claim is invalid")
        return await self._finalize_claim(
            record,
            owner=owner,
            lease_token=lease_token,
            current=_as_utc(now or datetime.now(UTC)),
            updates={
                "status": ExecutionTicketStatus.ISSUED,
                "run_state_version": run_state_version,
                "event_sequence": event_sequence,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
            },
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
        record = await self.store.get(ticket_hash_value)
        current = _as_utc(now or datetime.now(UTC))
        if record is None:
            raise ExecutionTicketError("Invalid execution ticket")
        if record.status == ExecutionTicketStatus.CONSUMED:
            if record.consumed_event_id == event_id:
                return record
            raise ExecutionTicketError("Execution ticket already consumed")
        try:
            return await self._finalize_claim(
                record,
                owner=owner,
                lease_token=lease_token,
                current=current,
                updates={
                    "status": ExecutionTicketStatus.CONSUMED,
                    "consumed_event_id": event_id,
                    "consumed_at": current,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                },
            )
        except ExecutionTicketError:
            canonical = await self.store.get(ticket_hash_value)
            if canonical is not None and canonical.status == ExecutionTicketStatus.CONSUMED:
                if canonical.consumed_event_id == event_id:
                    return canonical
                raise ExecutionTicketError("Execution ticket already consumed") from None
            raise

    async def _finalize_claim(
        self,
        record: ExecutionTicketRecord | None,
        *,
        owner: str,
        lease_token: str,
        current: datetime,
        updates: dict[str, object],
    ) -> ExecutionTicketRecord:
        if (
            record is None
            or record.status != ExecutionTicketStatus.CLAIMED
            or record.lease_owner != owner
            or record.lease_token != lease_token
            or record.lease_expires_at is None
            or record.lease_expires_at <= current
        ):
            raise ExecutionTicketError("Execution ticket claim is invalid")
        finalized = await self.store.finalize_claim(
            record.model_copy(update=updates),
            owner=owner,
            lease_token=lease_token,
            now=current,
        )
        if finalized is None:
            raise ExecutionTicketError("Execution ticket claim is invalid")
        return finalized

    def _encode(self, claims: ExecutionTicketClaims) -> str:
        raw = secrets.token_urlsafe(32)
        if self.secret:
            raw = hmac.new(
                self.secret.encode(),
                f"execution-ticket:{claims.nonce}".encode(),
                hashlib.sha256,
            ).hexdigest()
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
