import asyncio
import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from weakref import WeakValueDictionary

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import MemoryFormationJobModel, MemoryFormationTurnModel
from app.repositories.json_utils import dumps, loads
from app.schemas.memory import (
    MemoryFormationJob,
    MemoryFormationJobStatus,
    MemoryFormationMode,
    MemoryFormationTrigger,
    MemoryFormationTurn,
)

_DATABASE_SESSION_LOCKS: WeakValueDictionary[tuple[str, str, str, str], asyncio.Lock] = (
    WeakValueDictionary()
)


class MemoryFormationTurnJobRepository:
    def __init__(self) -> None:
        self.turns: dict[str, MemoryFormationTurn] = {}
        self.jobs: dict[str, MemoryFormationJob] = {}
        self.jobs_by_idempotency: dict[str, str] = {}
        self.watermarks: dict[tuple[str, str, str], str] = {}
        self._lock = asyncio.Lock()

    async def append_turn(self, turn: MemoryFormationTurn) -> MemoryFormationTurn:
        async with self._lock:
            existing = self.turns.get(turn.turn_id) or next(
                (
                    value
                    for value in self.turns.values()
                    if value.request_id == turn.request_id
                    and value.tenant_id == turn.tenant_id
                    and value.user_id == turn.user_id
                    and value.session_id == turn.session_id
                ),
                None,
            )
            if existing is not None:
                _validate_turn_identity(existing, turn)
                return existing.model_copy(deep=True)
            stored = turn.model_copy(deep=True)
            self.turns[turn.turn_id] = stored
            return stored.model_copy(deep=True)

    async def add_job(self, job: MemoryFormationJob) -> MemoryFormationJob:
        async with self._lock:
            existing_id = self.jobs_by_idempotency.get(job.idempotency_key)
            if existing_id is not None:
                existing = self.jobs[existing_id]
                _validate_job_identity(existing, job)
                return existing.model_copy(deep=True)
            existing = self.jobs.get(job.job_id)
            if existing is not None:
                _validate_job_identity(existing, job)
                return existing.model_copy(deep=True)
            stored = job.model_copy(deep=True)
            self.jobs[job.job_id] = stored
            self.jobs_by_idempotency[job.idempotency_key] = job.job_id
            return stored.model_copy(deep=True)

    async def list_turns_by_ids(
        self,
        turn_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[MemoryFormationTurn]:
        values = []
        for turn_id in turn_ids:
            turn = self.turns.get(turn_id)
            if turn is None or turn.tenant_id != tenant_id or turn.user_id != user_id:
                continue
            values.append(turn.model_copy(deep=True))
        return values

    async def append_turn_and_maybe_create_window_job(
        self,
        turn: MemoryFormationTurn,
        *,
        window_turns: int,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_attempts: int = 5,
    ) -> tuple[MemoryFormationTurn, MemoryFormationJob | None]:
        async with self._lock:
            existing = self.turns.get(turn.turn_id) or next(
                (
                    value
                    for value in self.turns.values()
                    if value.request_id == turn.request_id
                    and value.tenant_id == turn.tenant_id
                    and value.user_id == turn.user_id
                    and value.session_id == turn.session_id
                ),
                None,
            )
            if existing is not None:
                _validate_turn_identity(existing, turn)
                return existing.model_copy(deep=True), None
            stored = turn.model_copy(deep=True)
            self.turns[stored.turn_id] = stored
            effective_deadline = max(
                (
                    pending.idle_deadline_at
                    for pending in self.turns.values()
                    if pending.tenant_id == stored.tenant_id
                    and pending.user_id == stored.user_id
                    and pending.session_id == stored.session_id
                    and pending.status == "pending"
                    and pending.claimed_job_id is None
                    and pending.idle_deadline_at is not None
                ),
                default=stored.idle_deadline_at,
            )
            for pending in list(self.turns.values()):
                if (
                    pending.tenant_id == stored.tenant_id
                    and pending.user_id == stored.user_id
                    and pending.session_id == stored.session_id
                    and pending.status == "pending"
                    and pending.claimed_job_id is None
                ):
                    self.turns[pending.turn_id] = pending.model_copy(
                        update={"idle_deadline_at": effective_deadline}
                    )
            pending = sorted(
                (
                    value
                    for value in self.turns.values()
                    if value.tenant_id == stored.tenant_id
                    and value.user_id == stored.user_id
                    and value.session_id == stored.session_id
                    and value.status == "pending"
                    and value.claimed_job_id is None
                ),
                key=lambda value: (value.completed_at, value.turn_id),
            )
            job = None
            if len(pending) >= window_turns:
                frozen = pending[:window_turns]
                job = _build_range_job(
                    frozen,
                    trigger=MemoryFormationTrigger.TURN_WINDOW,
                    mode=mode,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    policy_version=policy_version,
                    max_attempts=max_attempts,
                )
                existing_id = self.jobs_by_idempotency.get(job.idempotency_key)
                if existing_id is not None:
                    job = self.jobs[existing_id]
                else:
                    self.jobs[job.job_id] = job.model_copy(deep=True)
                    self.jobs_by_idempotency[job.idempotency_key] = job.job_id
                for pending_turn in frozen:
                    self.turns[pending_turn.turn_id] = pending_turn.model_copy(
                        update={"status": "claimed", "claimed_job_id": job.job_id}
                    )
            return stored.model_copy(deep=True), job.model_copy(deep=True) if job else None

    async def list_pending_turns(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> list[MemoryFormationTurn]:
        values = [
            turn
            for turn in self.turns.values()
            if turn.tenant_id == tenant_id
            and turn.user_id == user_id
            and turn.session_id == session_id
            and turn.status == "pending"
            and turn.claimed_job_id is None
        ]
        return [
            turn.model_copy(deep=True)
            for turn in sorted(values, key=lambda item: (item.completed_at, item.turn_id))
        ]

    async def create_job_for_pending(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        trigger: MemoryFormationTrigger,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_turns: int | None = None,
        max_attempts: int = 5,
        idle_due_at: datetime | None = None,
    ) -> MemoryFormationJob | None:
        async with self._lock:
            pending = await self.list_pending_turns(
                tenant_id=tenant_id, user_id=user_id, session_id=session_id
            )
            if max_turns is not None:
                pending = pending[:max_turns]
            if not pending:
                return None
            if idle_due_at is not None and any(
                turn.idle_deadline_at is None or turn.idle_deadline_at > idle_due_at
                for turn in pending
            ):
                return None
            first_turn_id = pending[0].turn_id
            last_turn_id = pending[-1].turn_id
            idempotency_key = formation_range_idempotency_key(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                first_turn_id=first_turn_id,
                last_turn_id=last_turn_id,
                policy_version=policy_version,
            )
            existing_id = self.jobs_by_idempotency.get(idempotency_key)
            if existing_id is not None:
                return self.jobs[existing_id].model_copy(deep=True)
            job = MemoryFormationJob(
                trigger=trigger,
                mode=MemoryFormationMode(mode),
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                first_turn_id=first_turn_id,
                last_turn_id=last_turn_id,
                source_refs=[turn.turn_id for turn in pending],
                idempotency_key=idempotency_key,
                model_version=model_version,
                prompt_version=prompt_version,
                policy_version=policy_version,
                max_attempts=max_attempts,
            )
            self.jobs[job.job_id] = job.model_copy(deep=True)
            self.jobs_by_idempotency[idempotency_key] = job.job_id
            for turn in pending:
                self.turns[turn.turn_id] = turn.model_copy(
                    update={"status": "claimed", "claimed_job_id": job.job_id}
                )
            return job.model_copy(deep=True)

    async def claim_job(
        self, *, owner: str, now, lease_seconds: float
    ) -> MemoryFormationJob | None:
        async with self._lock:
            for job in list(self.jobs.values()):
                if (
                    job.status == MemoryFormationJobStatus.CLAIMED
                    and job.lease_expires_at is not None
                    and job.lease_expires_at <= now
                    and job.attempt_count >= job.max_attempts
                ):
                    self.jobs[job.job_id] = job.model_copy(
                        update={
                            "status": MemoryFormationJobStatus.DEAD_LETTER,
                            "lease_owner": None,
                            "lease_token": None,
                            "lease_expires_at": None,
                            "last_error_code": "lease_expired_attempts_exhausted",
                            "updated_at": now,
                        }
                    )
            eligible = [
                job
                for job in self.jobs.values()
                if job.attempt_count < job.max_attempts
                and (
                    job.status == MemoryFormationJobStatus.PENDING
                    or (
                        job.status == MemoryFormationJobStatus.RETRY
                        and (job.next_attempt_at is None or job.next_attempt_at <= now)
                    )
                    or (
                        job.status == MemoryFormationJobStatus.CLAIMED
                        and job.lease_expires_at is not None
                        and job.lease_expires_at <= now
                    )
                )
            ]
            if not eligible:
                return None
            job = sorted(eligible, key=lambda item: (item.created_at, item.job_id))[0]
            claimed = job.model_copy(
                update={
                    "status": MemoryFormationJobStatus.CLAIMED,
                    "lease_owner": owner,
                    "lease_token": f"lease_{uuid4().hex}",
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "attempt_count": job.attempt_count + 1,
                    "updated_at": now,
                }
            )
            self.jobs[job.job_id] = claimed
            return claimed.model_copy(deep=True)

    async def complete_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_token: str,
        now,
        trace_summary: dict | None = None,
    ) -> MemoryFormationJob:
        async with self._lock:
            job = self._required_job(job_id)
            self._validate_claim(job, owner=owner, lease_token=lease_token, now=now)
            completed = job.model_copy(
                update={
                    "status": MemoryFormationJobStatus.COMPLETED,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "trace_summary": {
                        **deepcopy(job.trace_summary),
                        **deepcopy(trace_summary or {}),
                    },
                    "updated_at": now,
                }
            )
            self.jobs[job_id] = completed
            for turn_id in job.source_refs:
                turn = self.turns.get(turn_id)
                if turn is None:
                    continue
                self.turns[turn_id] = turn.model_copy(update={"status": "formed"})
            if job.session_id:
                self._advance_contiguous_watermark(
                    tenant_id=job.tenant_id,
                    user_id=job.user_id,
                    session_id=job.session_id,
                )
            return completed.model_copy(deep=True)

    async def fail_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_token: str,
        now,
        error_code: str,
        next_attempt_at,
    ) -> MemoryFormationJob:
        async with self._lock:
            job = self._required_job(job_id)
            self._validate_claim(job, owner=owner, lease_token=lease_token, now=now)
            status = (
                MemoryFormationJobStatus.DEAD_LETTER
                if job.attempt_count >= job.max_attempts
                else MemoryFormationJobStatus.RETRY
            )
            failed = job.model_copy(
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
            self.jobs[job_id] = failed
            return failed.model_copy(deep=True)

    async def successful_watermark(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> str | None:
        return self.watermarks.get((tenant_id, user_id, session_id))

    async def list_idle_sessions(
        self, *, now: datetime, limit: int = 100
    ) -> list[tuple[str, str, str]]:
        deadlines: dict[tuple[str, str, str], datetime] = {}
        for turn in self.turns.values():
            if (
                turn.status != "pending"
                or turn.claimed_job_id is not None
                or turn.idle_deadline_at is None
            ):
                continue
            identity = (turn.tenant_id, turn.user_id, turn.session_id)
            deadlines[identity] = max(
                deadlines.get(identity, turn.idle_deadline_at), turn.idle_deadline_at
            )
        return sorted(identity for identity, deadline in deadlines.items() if deadline <= now)[
            :limit
        ]

    def _required_job(self, job_id: str) -> MemoryFormationJob:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise ValueError("Formation job not found") from exc

    @staticmethod
    def _validate_claim(job, *, owner: str, lease_token: str, now) -> None:
        if job.status != MemoryFormationJobStatus.CLAIMED:
            raise ValueError("Formation job is not claimed")
        if job.lease_owner != owner or job.lease_token != lease_token:
            raise ValueError("Formation job lease ownership changed")
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            raise ValueError("Formation job lease expired")

    def _advance_contiguous_watermark(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> None:
        turns = sorted(
            (
                turn
                for turn in self.turns.values()
                if turn.tenant_id == tenant_id
                and turn.user_id == user_id
                and turn.session_id == session_id
            ),
            key=lambda item: (item.completed_at, item.turn_id),
        )
        contiguous_last = None
        for turn in turns:
            if turn.status not in {"formed", "skipped"}:
                break
            contiguous_last = turn.turn_id
        if contiguous_last is not None:
            self.watermarks[(tenant_id, user_id, session_id)] = contiguous_last


def formation_range_idempotency_key(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    first_turn_id: str,
    last_turn_id: str,
    policy_version: str,
) -> str:
    value = "\x1f".join(
        (tenant_id, user_id, session_id, first_turn_id, last_turn_id, policy_version)
    )
    return f"formation-range:{hashlib.sha256(value.encode()).hexdigest()}"


class DatabaseMemoryFormationTurnJobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def append_turn(self, turn: MemoryFormationTurn) -> MemoryFormationTurn:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(MemoryFormationTurnModel).where(
                    or_(
                        MemoryFormationTurnModel.turn_id == turn.turn_id,
                        and_(
                            MemoryFormationTurnModel.request_id == turn.request_id,
                            MemoryFormationTurnModel.tenant_id == turn.tenant_id,
                            MemoryFormationTurnModel.user_id == turn.user_id,
                            MemoryFormationTurnModel.session_id == turn.session_id,
                        ),
                    )
                )
            )
            if existing is not None:
                _validate_turn_identity(existing, turn)
                return _turn_from_row(existing)
            row = MemoryFormationTurnModel(**_turn_values(turn))
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(MemoryFormationTurnModel).where(
                        MemoryFormationTurnModel.request_id == turn.request_id,
                        MemoryFormationTurnModel.tenant_id == turn.tenant_id,
                        MemoryFormationTurnModel.user_id == turn.user_id,
                        MemoryFormationTurnModel.session_id == turn.session_id,
                    )
                )
                if existing is None:
                    raise
                _validate_turn_identity(existing, turn)
                return _turn_from_row(existing)
            await session.refresh(row)
            return _turn_from_row(row)

    async def add_job(self, job: MemoryFormationJob) -> MemoryFormationJob:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(MemoryFormationJobModel).where(
                    MemoryFormationJobModel.idempotency_key == job.idempotency_key
                )
            )
            if existing is not None:
                stored = _job_from_row(existing)
                _validate_job_identity(stored, job)
                return stored
            by_id = await session.get(MemoryFormationJobModel, job.job_id)
            if by_id is not None:
                stored = _job_from_row(by_id)
                _validate_job_identity(stored, job)
                return stored
            session.add(MemoryFormationJobModel(**_job_values(job)))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(MemoryFormationJobModel).where(
                        MemoryFormationJobModel.idempotency_key == job.idempotency_key
                    )
                )
                if existing is None:
                    raise
                stored = _job_from_row(existing)
                _validate_job_identity(stored, job)
                return stored
            return job.model_copy(deep=True)

    async def list_turns_by_ids(
        self,
        turn_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[MemoryFormationTurn]:
        if not turn_ids:
            return []
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryFormationTurnModel).where(
                            MemoryFormationTurnModel.turn_id.in_(turn_ids),
                            MemoryFormationTurnModel.tenant_id == tenant_id,
                            MemoryFormationTurnModel.user_id == user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_id = {row.turn_id: _turn_from_row(row) for row in rows}
        return [by_id[turn_id] for turn_id in turn_ids if turn_id in by_id]

    async def append_turn_and_maybe_create_window_job(
        self,
        turn: MemoryFormationTurn,
        *,
        window_turns: int,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_attempts: int = 5,
    ) -> tuple[MemoryFormationTurn, MemoryFormationJob | None]:
        async with self._session_lock(
            tenant_id=turn.tenant_id,
            user_id=turn.user_id,
            session_id=turn.session_id,
        ):
            return await self._append_turn_and_maybe_create_window_job(
                turn,
                window_turns=window_turns,
                mode=mode,
                model_version=model_version,
                prompt_version=prompt_version,
                policy_version=policy_version,
                max_attempts=max_attempts,
            )

    async def _append_turn_and_maybe_create_window_job(
        self,
        turn: MemoryFormationTurn,
        *,
        window_turns: int,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_attempts: int = 5,
    ) -> tuple[MemoryFormationTurn, MemoryFormationJob | None]:
        async with self.session_factory() as session:
            await _lock_formation_session(
                session,
                tenant_id=turn.tenant_id,
                user_id=turn.user_id,
                session_id=turn.session_id,
            )
            existing = await session.scalar(
                select(MemoryFormationTurnModel).where(
                    or_(
                        MemoryFormationTurnModel.turn_id == turn.turn_id,
                        and_(
                            MemoryFormationTurnModel.request_id == turn.request_id,
                            MemoryFormationTurnModel.tenant_id == turn.tenant_id,
                            MemoryFormationTurnModel.user_id == turn.user_id,
                            MemoryFormationTurnModel.session_id == turn.session_id,
                        ),
                    )
                )
            )
            if existing is not None:
                _validate_turn_identity(existing, turn)
                return _turn_from_row(existing), None
            row = MemoryFormationTurnModel(**_turn_values(turn))
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(MemoryFormationTurnModel).where(
                        or_(
                            MemoryFormationTurnModel.turn_id == turn.turn_id,
                            and_(
                                MemoryFormationTurnModel.request_id == turn.request_id,
                                MemoryFormationTurnModel.tenant_id == turn.tenant_id,
                                MemoryFormationTurnModel.user_id == turn.user_id,
                                MemoryFormationTurnModel.session_id == turn.session_id,
                            ),
                        )
                    )
                )
                if existing is None:
                    raise
                _validate_turn_identity(existing, turn)
                job = (
                    await session.get(MemoryFormationJobModel, existing.claimed_job_id)
                    if existing.claimed_job_id
                    else None
                )
                return _turn_from_row(existing), _job_from_row(job) if job else None
            latest_deadline = await session.scalar(
                select(func.max(MemoryFormationTurnModel.idle_deadline_at)).where(
                    MemoryFormationTurnModel.tenant_id == turn.tenant_id,
                    MemoryFormationTurnModel.user_id == turn.user_id,
                    MemoryFormationTurnModel.session_id == turn.session_id,
                    MemoryFormationTurnModel.status == "pending",
                    MemoryFormationTurnModel.claimed_job_id.is_(None),
                    MemoryFormationTurnModel.idle_deadline_at.is_not(None),
                )
            )
            effective_deadline = max(
                value
                for value in (_as_utc(latest_deadline), turn.idle_deadline_at)
                if value is not None
            )
            await session.execute(
                update(MemoryFormationTurnModel)
                .where(
                    MemoryFormationTurnModel.tenant_id == turn.tenant_id,
                    MemoryFormationTurnModel.user_id == turn.user_id,
                    MemoryFormationTurnModel.session_id == turn.session_id,
                    MemoryFormationTurnModel.status == "pending",
                    MemoryFormationTurnModel.claimed_job_id.is_(None),
                )
                .values(idle_deadline_at=effective_deadline)
            )
            pending = (
                (
                    await session.execute(
                        select(MemoryFormationTurnModel)
                        .where(
                            MemoryFormationTurnModel.tenant_id == turn.tenant_id,
                            MemoryFormationTurnModel.user_id == turn.user_id,
                            MemoryFormationTurnModel.session_id == turn.session_id,
                            MemoryFormationTurnModel.status == "pending",
                            MemoryFormationTurnModel.claimed_job_id.is_(None),
                        )
                        .order_by(
                            MemoryFormationTurnModel.completed_at,
                            MemoryFormationTurnModel.turn_id,
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            job = None
            if len(pending) >= window_turns:
                frozen = pending[:window_turns]
                job = _build_range_job(
                    [_turn_from_row(value) for value in frozen],
                    trigger=MemoryFormationTrigger.TURN_WINDOW,
                    mode=mode,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    policy_version=policy_version,
                    max_attempts=max_attempts,
                )
                existing_job = await session.scalar(
                    select(MemoryFormationJobModel).where(
                        MemoryFormationJobModel.idempotency_key == job.idempotency_key
                    )
                )
                if existing_job is not None:
                    job = _job_from_row(existing_job)
                else:
                    session.add(MemoryFormationJobModel(**_job_values(job)))
                for pending_turn in frozen:
                    pending_turn.status = "claimed"
                    pending_turn.claimed_job_id = job.job_id
            await session.commit()
            await session.refresh(row)
            return _turn_from_row(row), job

    async def list_pending_turns(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> list[MemoryFormationTurn]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryFormationTurnModel)
                        .where(
                            MemoryFormationTurnModel.tenant_id == tenant_id,
                            MemoryFormationTurnModel.user_id == user_id,
                            MemoryFormationTurnModel.session_id == session_id,
                            MemoryFormationTurnModel.status == "pending",
                            MemoryFormationTurnModel.claimed_job_id.is_(None),
                        )
                        .order_by(
                            MemoryFormationTurnModel.completed_at,
                            MemoryFormationTurnModel.turn_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_turn_from_row(row) for row in rows]

    async def create_job_for_pending(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        trigger: MemoryFormationTrigger,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_turns: int | None = None,
        max_attempts: int = 5,
        idle_due_at: datetime | None = None,
    ) -> MemoryFormationJob | None:
        async with self._session_lock(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        ):
            return await self._create_job_for_pending(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                trigger=trigger,
                mode=mode,
                model_version=model_version,
                prompt_version=prompt_version,
                policy_version=policy_version,
                max_turns=max_turns,
                max_attempts=max_attempts,
                idle_due_at=idle_due_at,
            )

    async def _create_job_for_pending(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        trigger: MemoryFormationTrigger,
        mode: str,
        model_version: str,
        prompt_version: str,
        policy_version: str,
        max_turns: int | None = None,
        max_attempts: int = 5,
        idle_due_at: datetime | None = None,
    ) -> MemoryFormationJob | None:
        async with self.session_factory() as session:
            await _lock_formation_session(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
            )
            stmt = (
                select(MemoryFormationTurnModel)
                .where(
                    MemoryFormationTurnModel.tenant_id == tenant_id,
                    MemoryFormationTurnModel.user_id == user_id,
                    MemoryFormationTurnModel.session_id == session_id,
                    MemoryFormationTurnModel.status == "pending",
                    MemoryFormationTurnModel.claimed_job_id.is_(None),
                )
                .order_by(
                    MemoryFormationTurnModel.completed_at,
                    MemoryFormationTurnModel.turn_id,
                )
                .with_for_update(skip_locked=True)
            )
            if max_turns is not None:
                stmt = stmt.limit(max_turns)
            turns = (await session.execute(stmt)).scalars().all()
            if not turns:
                return None
            if idle_due_at is not None and any(
                turn.idle_deadline_at is None or _as_utc(turn.idle_deadline_at) > idle_due_at
                for turn in turns
            ):
                return None
            idempotency_key = formation_range_idempotency_key(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                first_turn_id=turns[0].turn_id,
                last_turn_id=turns[-1].turn_id,
                policy_version=policy_version,
            )
            existing = await session.scalar(
                select(MemoryFormationJobModel).where(
                    MemoryFormationJobModel.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return _job_from_row(existing)
            job = MemoryFormationJob(
                trigger=trigger,
                mode=MemoryFormationMode(mode),
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                first_turn_id=turns[0].turn_id,
                last_turn_id=turns[-1].turn_id,
                source_refs=[turn.turn_id for turn in turns],
                idempotency_key=idempotency_key,
                model_version=model_version,
                prompt_version=prompt_version,
                policy_version=policy_version,
                max_attempts=max_attempts,
            )
            session.add(MemoryFormationJobModel(**_job_values(job)))
            for turn in turns:
                turn.status = "claimed"
                turn.claimed_job_id = job.job_id
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(MemoryFormationJobModel).where(
                        MemoryFormationJobModel.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise
                return _job_from_row(existing)
            return job

    def _session_lock(self, *, tenant_id: str, user_id: str, session_id: str) -> asyncio.Lock:
        bind = self.session_factory.kw.get("bind")
        database_identity = str(bind.url) if bind is not None else str(id(bind))
        identity = (database_identity, tenant_id, user_id, session_id)
        return _DATABASE_SESSION_LOCKS.setdefault(identity, asyncio.Lock())

    async def claim_job(
        self, *, owner: str, now: datetime, lease_seconds: float
    ) -> MemoryFormationJob | None:
        async with self.session_factory() as session:
            exhausted = (
                (
                    await session.execute(
                        select(MemoryFormationJobModel)
                        .where(
                            MemoryFormationJobModel.status == "claimed",
                            MemoryFormationJobModel.lease_expires_at <= now,
                            MemoryFormationJobModel.attempt_count
                            >= MemoryFormationJobModel.max_attempts,
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in exhausted:
                row.status = "dead_letter"
                row.lease_owner = None
                row.lease_token = None
                row.lease_expires_at = None
                row.last_error_code = "lease_expired_attempts_exhausted"
                row.updated_at = now
            row = await session.scalar(
                select(MemoryFormationJobModel)
                .where(
                    MemoryFormationJobModel.attempt_count < MemoryFormationJobModel.max_attempts,
                    or_(
                        MemoryFormationJobModel.status == "pending",
                        (
                            (MemoryFormationJobModel.status == "retry")
                            & or_(
                                MemoryFormationJobModel.next_attempt_at.is_(None),
                                MemoryFormationJobModel.next_attempt_at <= now,
                            )
                        ),
                        (
                            (MemoryFormationJobModel.status == "claimed")
                            & (MemoryFormationJobModel.lease_expires_at <= now)
                        ),
                    ),
                )
                .order_by(
                    MemoryFormationJobModel.created_at,
                    MemoryFormationJobModel.job_id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                await session.commit()
                return None
            lease_token = f"lease_{uuid4().hex}"
            result = await session.execute(
                update(MemoryFormationJobModel)
                .where(
                    MemoryFormationJobModel.job_id == row.job_id,
                    MemoryFormationJobModel.status == row.status,
                    MemoryFormationJobModel.attempt_count == row.attempt_count,
                )
                .values(
                    status="claimed",
                    lease_owner=owner,
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempt_count=row.attempt_count + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            await session.refresh(row)
            return _job_from_row(row)

    async def complete_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        trace_summary: dict | None = None,
    ) -> MemoryFormationJob:
        async with self.session_factory() as session:
            row = await session.get(MemoryFormationJobModel, job_id)
            if row is None:
                raise ValueError("Formation job not found")
            refs = loads(row.source_refs_text, [])
            result = await session.execute(
                update(MemoryFormationJobModel)
                .where(
                    MemoryFormationJobModel.job_id == job_id,
                    MemoryFormationJobModel.status == "claimed",
                    MemoryFormationJobModel.lease_owner == owner,
                    MemoryFormationJobModel.lease_token == lease_token,
                    MemoryFormationJobModel.lease_expires_at > now,
                )
                .values(
                    status="completed",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    trace_summary_text=dumps(
                        {
                            **loads(row.trace_summary_text, {}),
                            **(trace_summary or {}),
                        }
                    ),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise ValueError("Formation job claim transition lost")
            await session.execute(
                update(MemoryFormationTurnModel)
                .where(MemoryFormationTurnModel.turn_id.in_(refs))
                .values(status="formed")
            )
            await session.commit()
            await session.refresh(row)
            return _job_from_row(row)

    async def fail_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        next_attempt_at: datetime,
    ) -> MemoryFormationJob:
        async with self.session_factory() as session:
            row = await session.get(MemoryFormationJobModel, job_id)
            if row is None:
                raise ValueError("Formation job not found")
            status = "dead_letter" if row.attempt_count >= row.max_attempts else "retry"
            result = await session.execute(
                update(MemoryFormationJobModel)
                .where(
                    MemoryFormationJobModel.job_id == job_id,
                    MemoryFormationJobModel.status == "claimed",
                    MemoryFormationJobModel.lease_owner == owner,
                    MemoryFormationJobModel.lease_token == lease_token,
                    MemoryFormationJobModel.lease_expires_at > now,
                )
                .values(
                    status=status,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    next_attempt_at=next_attempt_at,
                    last_error_code=error_code,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise ValueError("Formation job claim transition lost")
            await session.commit()
            await session.refresh(row)
            return _job_from_row(row)

    async def successful_watermark(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> str | None:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryFormationTurnModel)
                        .where(
                            MemoryFormationTurnModel.tenant_id == tenant_id,
                            MemoryFormationTurnModel.user_id == user_id,
                            MemoryFormationTurnModel.session_id == session_id,
                        )
                        .order_by(
                            MemoryFormationTurnModel.completed_at,
                            MemoryFormationTurnModel.turn_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            watermark = None
            for row in rows:
                if row.status not in {"formed", "skipped"}:
                    break
                watermark = row.turn_id
            return watermark

    async def list_idle_sessions(
        self, *, now: datetime, limit: int = 100
    ) -> list[tuple[str, str, str]]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        MemoryFormationTurnModel.tenant_id,
                        MemoryFormationTurnModel.user_id,
                        MemoryFormationTurnModel.session_id,
                    )
                    .where(
                        MemoryFormationTurnModel.status == "pending",
                        MemoryFormationTurnModel.claimed_job_id.is_(None),
                        MemoryFormationTurnModel.idle_deadline_at.is_not(None),
                    )
                    .group_by(
                        MemoryFormationTurnModel.tenant_id,
                        MemoryFormationTurnModel.user_id,
                        MemoryFormationTurnModel.session_id,
                    )
                    .having(func.max(MemoryFormationTurnModel.idle_deadline_at) <= now)
                    .order_by(func.max(MemoryFormationTurnModel.idle_deadline_at))
                    .limit(limit)
                )
            ).all()
            return [(row.tenant_id, row.user_id, row.session_id) for row in rows]


def _build_range_job(
    turns: list[MemoryFormationTurn],
    *,
    trigger: MemoryFormationTrigger,
    mode: str,
    model_version: str,
    prompt_version: str,
    policy_version: str,
    max_attempts: int,
) -> MemoryFormationJob:
    first = turns[0]
    last = turns[-1]
    return MemoryFormationJob(
        trigger=trigger,
        mode=MemoryFormationMode(mode),
        tenant_id=first.tenant_id,
        user_id=first.user_id,
        session_id=first.session_id,
        first_turn_id=first.turn_id,
        last_turn_id=last.turn_id,
        source_refs=[turn.turn_id for turn in turns],
        idempotency_key=formation_range_idempotency_key(
            tenant_id=first.tenant_id,
            user_id=first.user_id,
            session_id=first.session_id,
            first_turn_id=first.turn_id,
            last_turn_id=last.turn_id,
            policy_version=policy_version,
        ),
        model_version=model_version,
        prompt_version=prompt_version,
        policy_version=policy_version,
        max_attempts=max_attempts,
    )


async def _lock_formation_session(
    session: AsyncSession, *, tenant_id: str, user_id: str, session_id: str
) -> None:
    if session.bind is None:
        return
    if session.bind.dialect.name == "sqlite":
        await session.execute(text("BEGIN IMMEDIATE"))
        return
    if session.bind.dialect.name != "postgresql":
        return
    identity = "\x1f".join((tenant_id, user_id, session_id))
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": identity},
    )


def _turn_values(turn: MemoryFormationTurn) -> dict:
    return {
        "turn_id": turn.turn_id,
        "request_id": turn.request_id,
        "session_id": turn.session_id,
        "run_id": turn.run_id,
        "user_id": turn.user_id,
        "tenant_id": turn.tenant_id,
        "agent_id": turn.agent_id,
        "user_text": turn.user_text,
        "assistant_text": turn.assistant_text,
        "result_status": turn.result_status,
        "source_refs_text": dumps(
            {
                "result": turn.result_refs,
                "plan": turn.plan_refs,
                "artifact": turn.artifact_refs,
            }
        ),
        "used_memory_ids_text": dumps(turn.used_memory_ids),
        "status": turn.status,
        "claimed_job_id": turn.claimed_job_id,
        "idle_deadline_at": turn.idle_deadline_at,
        "completed_at": turn.completed_at,
        "created_at": turn.created_at,
    }


def _turn_from_row(row) -> MemoryFormationTurn:
    refs = loads(row.source_refs_text, {})
    if isinstance(refs, list):
        refs = {"result": refs, "plan": [], "artifact": []}
    return MemoryFormationTurn(
        turn_id=row.turn_id,
        request_id=row.request_id,
        session_id=row.session_id,
        run_id=row.run_id,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        user_text=row.user_text,
        assistant_text=row.assistant_text,
        result_status=row.result_status,
        result_refs=refs.get("result", []),
        plan_refs=refs.get("plan", []),
        artifact_refs=refs.get("artifact", []),
        used_memory_ids=loads(row.used_memory_ids_text, []),
        status=row.status,
        claimed_job_id=row.claimed_job_id,
        idle_deadline_at=_as_utc(row.idle_deadline_at),
        completed_at=_as_utc(row.completed_at),
        created_at=_as_utc(row.created_at),
    )


def _job_values(job: MemoryFormationJob) -> dict:
    return {
        "job_id": job.job_id,
        "trigger": job.trigger.value,
        "status": job.status.value,
        "mode": job.mode.value,
        "tenant_id": job.tenant_id,
        "user_id": job.user_id,
        "session_id": job.session_id,
        "first_turn_id": job.first_turn_id,
        "last_turn_id": job.last_turn_id,
        "source_refs_text": dumps(job.source_refs),
        "idempotency_key": job.idempotency_key,
        "model_version": job.model_version,
        "prompt_version": job.prompt_version,
        "policy_version": job.policy_version,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "lease_owner": job.lease_owner,
        "lease_token": job.lease_token,
        "lease_expires_at": job.lease_expires_at,
        "next_attempt_at": job.next_attempt_at,
        "last_error_code": job.last_error_code,
        "trace_summary_text": dumps(job.trace_summary),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _job_from_row(row) -> MemoryFormationJob:
    return MemoryFormationJob(
        job_id=row.job_id,
        trigger=row.trigger,
        status=row.status,
        mode=row.mode,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        session_id=row.session_id,
        first_turn_id=row.first_turn_id,
        last_turn_id=row.last_turn_id,
        source_refs=loads(row.source_refs_text, []),
        idempotency_key=row.idempotency_key,
        model_version=row.model_version,
        prompt_version=row.prompt_version,
        policy_version=row.policy_version,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=_as_utc(row.lease_expires_at),
        next_attempt_at=_as_utc(row.next_attempt_at),
        last_error_code=row.last_error_code,
        trace_summary=loads(row.trace_summary_text, {}),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _validate_turn_identity(row, turn: MemoryFormationTurn) -> None:
    persisted = (row.turn_id, row.request_id, row.tenant_id, row.user_id, row.session_id)
    incoming = (
        turn.turn_id,
        turn.request_id,
        turn.tenant_id,
        turn.user_id,
        turn.session_id,
    )
    if persisted != incoming:
        raise ValueError("Formation turn identity conflict")


def _validate_job_identity(existing: MemoryFormationJob, incoming: MemoryFormationJob) -> None:
    fields = (
        "trigger",
        "tenant_id",
        "user_id",
        "session_id",
        "source_refs",
        "idempotency_key",
        "model_version",
        "prompt_version",
        "policy_version",
    )
    if any(getattr(existing, field) != getattr(incoming, field) for field in fields):
        raise ValueError("Formation job identity conflict")
    if existing.trace_summary.get("command") != incoming.trace_summary.get("command"):
        raise ValueError("Formation job command conflict")
