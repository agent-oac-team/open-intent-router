from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    AgentResultModel,
    AgentRunModel,
    CanonicalTurnModel,
    MemoryEventModel,
    TurnOutboxModel,
)
from app.repositories.json_utils import dumps, loads
from app.schemas.common import StrictBaseModel

OrphanTurnCategory = Literal[
    "repairable_enabled",
    "repairable_skipped",
    "ambiguous",
    "ownership_conflict",
]

_ACTIVE_TURN_STATUSES = ("pending", "routing", "running", "blocked")
_TERMINAL_RUN_STATUSES = ("completed", "failed", "invalid_output", "timeout", "timed_out")
_TERMINAL_RESULT_STATUSES = _TERMINAL_RUN_STATUSES


class OrphanTurnRecord(StrictBaseModel):
    turn_id: str
    request_id: str
    category: OrphanTurnCategory
    reason_code: str = Field(max_length=128)
    run_id: str | None = None
    result_id: str | None = None
    result_status: str | None = None


class OrphanTurnReport(StrictBaseModel):
    contract: Literal["orphan-turn-reconcile/v1"] = "orphan-turn-reconcile/v1"
    dry_run: bool = True
    tenant_id: str
    user_id: str
    records: list[OrphanTurnRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class OrphanTurnRepairResult(StrictBaseModel):
    turn_id: str
    request_id: str
    category: OrphanTurnCategory
    action: Literal["completed_with_outbox", "completed_skipped", "idempotent_replay"]
    outbox_id: str | None = None
    audit_event_id: str | None = None
    idempotent_replay: bool = False


class OrphanTurnRepairConflict(ValueError):
    pass


class DatabaseOrphanTurnReconciler:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def scan(
        self,
        *,
        tenant_id: str,
        user_id: str,
        request_ids: list[str] | None = None,
        updated_before: datetime | None = None,
        limit: int = 500,
    ) -> OrphanTurnReport:
        limit = max(1, min(limit, 1000))
        async with self.session_factory() as session:
            statement = (
                select(CanonicalTurnModel)
                .where(
                    CanonicalTurnModel.tenant_id == tenant_id,
                    CanonicalTurnModel.user_id == user_id,
                    CanonicalTurnModel.status.in_(_ACTIVE_TURN_STATUSES),
                )
                .order_by(CanonicalTurnModel.updated_at, CanonicalTurnModel.turn_id)
                .limit(limit)
            )
            if request_ids:
                statement = statement.where(CanonicalTurnModel.request_id.in_(request_ids[:1000]))
            if updated_before is not None:
                statement = statement.where(CanonicalTurnModel.updated_at <= updated_before)
            turns = (await session.execute(statement)).scalars().all()
            records = [await self._classify_turn(session, turn) for turn in turns]
        return OrphanTurnReport(
            tenant_id=tenant_id,
            user_id=user_id,
            records=records,
            counts=dict(sorted(Counter(record.category for record in records).items())),
        )

    async def repair(
        self,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
        idempotency_key: str,
    ) -> OrphanTurnRepairResult:
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must contain 1-256 characters")
        repair_token = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        async with self.session_factory() as session, session.begin():
            turn = await session.scalar(
                select(CanonicalTurnModel)
                .where(
                    CanonicalTurnModel.tenant_id == tenant_id,
                    CanonicalTurnModel.user_id == user_id,
                    CanonicalTurnModel.request_id == request_id,
                )
                .with_for_update()
            )
            if turn is None:
                raise OrphanTurnRepairConflict("Canonical Turn not found in owner scope")
            if turn.status not in _ACTIVE_TURN_STATUSES:
                replay = await self._repair_replay(session, turn, repair_token)
                if replay is not None:
                    return replay
                raise OrphanTurnRepairConflict("Canonical Turn is already terminal")

            record = await self._classify_turn(session, turn)
            if record.category not in {"repairable_enabled", "repairable_skipped"}:
                raise OrphanTurnRepairConflict(
                    f"Canonical Turn is not repairable: {record.reason_code}"
                )
            run = await session.get(AgentRunModel, record.run_id)
            result = await session.get(AgentResultModel, record.result_id)
            if run is None or result is None:
                raise OrphanTurnRepairConflict("Repair association changed concurrently")

            now = datetime.now(UTC)
            references = loads(turn.references_text, {})
            references.setdefault("run_ids", [])
            references.setdefault("result_ids", [])
            if run.run_id not in references["run_ids"]:
                references["run_ids"].append(run.run_id)
            if result.result_id not in references["result_ids"]:
                references["result_ids"].append(result.result_id)
            terminal_status = "completed" if result.status == "completed" else "failed"
            turn.status = terminal_status
            turn.state_version += 1
            turn.references_text = dumps(references)
            turn.final_response_text = dumps(
                {
                    "kind": "agent_result" if terminal_status == "completed" else "error",
                    "text": result.message or "",
                    "output": loads(result.output_text, None),
                    "error": loads(result.error_text, None),
                }
            )
            turn.updated_at = now
            turn.completed_at = now
            run.turn_id = turn.turn_id
            result.turn_id = turn.turn_id
            result.run_state_version = run.state_version

            if record.category == "repairable_enabled":
                outbox_id = (
                    f"outbox_repair_{hashlib.sha256(turn.turn_id.encode()).hexdigest()[:24]}"
                )
                session.add(
                    TurnOutboxModel(
                        outbox_id=outbox_id,
                        turn_id=turn.turn_id,
                        event_type="turn.completed",
                        idempotency_key=f"turn.completed:{turn.turn_id}",
                        payload_text=dumps(
                            {
                                "turn_id": turn.turn_id,
                                "tenant_id": tenant_id,
                                "user_id": user_id,
                                "request_id": request_id,
                                "run_id": run.run_id,
                                "result_id": result.result_id,
                                "state_version": turn.state_version,
                                "repair_token": repair_token,
                                "formation_eligibility": {
                                    "mode": "enforced",
                                    "execution_mode": "live",
                                    "suppressed": False,
                                    "reason_code": None,
                                    "policy_version": "orphan-reconciler-v1",
                                },
                            }
                        ),
                        available_at=now,
                    )
                )
                return OrphanTurnRepairResult(
                    turn_id=turn.turn_id,
                    request_id=request_id,
                    category=record.category,
                    action="completed_with_outbox",
                    outbox_id=outbox_id,
                )

            audit_event_id = (
                f"mevt_orphan_skip_{hashlib.sha256(turn.turn_id.encode()).hexdigest()[:24]}"
            )
            session.add(
                MemoryEventModel(
                    event_id=audit_event_id,
                    event_type="formation_skipped",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    request_id=request_id,
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    run_id=run.run_id,
                    payload_text=dumps(
                        {
                            "reason_code": record.reason_code,
                            "repair_token": repair_token,
                            "source": "orphan_turn_reconciler",
                        }
                    ),
                )
            )
            return OrphanTurnRepairResult(
                turn_id=turn.turn_id,
                request_id=request_id,
                category=record.category,
                action="completed_skipped",
                audit_event_id=audit_event_id,
            )

    async def _classify_turn(
        self, session: AsyncSession, turn: CanonicalTurnModel
    ) -> OrphanTurnRecord:
        existing_outbox = await session.scalar(
            select(TurnOutboxModel.outbox_id)
            .where(TurnOutboxModel.turn_id == turn.turn_id)
            .limit(1)
        )
        if existing_outbox:
            return self._record(turn, "ambiguous", "active_turn_has_outbox")

        runs = (
            (
                await session.execute(
                    select(AgentRunModel).where(
                        AgentRunModel.request_id == turn.request_id,
                        AgentRunModel.status.in_(_TERMINAL_RUN_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(
            run.tenant_id != turn.tenant_id
            or run.user_id != turn.user_id
            or run.session_id != turn.session_id
            for run in runs
        ):
            return self._record(turn, "ownership_conflict", "run_ownership_conflict")
        if not runs:
            return self._record(turn, "ambiguous", "terminal_run_missing")

        results = (
            (
                await session.execute(
                    select(AgentResultModel).where(
                        AgentResultModel.run_id.in_([run.run_id for run in runs]),
                        AgentResultModel.status.in_(_TERMINAL_RESULT_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(
            result.tenant_id != turn.tenant_id
            or result.user_id != turn.user_id
            or result.session_id != turn.session_id
            for result in results
        ):
            return self._record(turn, "ownership_conflict", "result_ownership_conflict")
        pairs = [(run, result) for run in runs for result in results if result.run_id == run.run_id]
        if len(pairs) != 1:
            return self._record(turn, "ambiguous", "terminal_association_not_unique")
        run, result = pairs[0]
        suppressed = run.formation_suppressed or result.formation_suppressed
        if suppressed or result.status != "completed":
            reason = "formation_suppressed" if suppressed else "terminal_result_not_completed"
            return self._record(
                turn,
                "repairable_skipped",
                reason,
                run=run,
                result=result,
            )
        return self._record(
            turn,
            "repairable_enabled",
            "unique_completed_result",
            run=run,
            result=result,
        )

    async def _repair_replay(
        self, session: AsyncSession, turn: CanonicalTurnModel, repair_token: str
    ) -> OrphanTurnRepairResult | None:
        outboxes = (
            (
                await session.execute(
                    select(TurnOutboxModel).where(TurnOutboxModel.turn_id == turn.turn_id)
                )
            )
            .scalars()
            .all()
        )
        for outbox in outboxes:
            if loads(outbox.payload_text, {}).get("repair_token") == repair_token:
                return OrphanTurnRepairResult(
                    turn_id=turn.turn_id,
                    request_id=turn.request_id,
                    category="repairable_enabled",
                    action="idempotent_replay",
                    outbox_id=outbox.outbox_id,
                    idempotent_replay=True,
                )
        events = (
            (
                await session.execute(
                    select(MemoryEventModel).where(
                        MemoryEventModel.turn_id == turn.turn_id,
                        MemoryEventModel.event_type == "formation_skipped",
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            if loads(event.payload_text, {}).get("repair_token") == repair_token:
                return OrphanTurnRepairResult(
                    turn_id=turn.turn_id,
                    request_id=turn.request_id,
                    category="repairable_skipped",
                    action="idempotent_replay",
                    audit_event_id=event.event_id,
                    idempotent_replay=True,
                )
        return None

    @staticmethod
    def _record(
        turn: CanonicalTurnModel,
        category: OrphanTurnCategory,
        reason_code: str,
        *,
        run: AgentRunModel | None = None,
        result: AgentResultModel | None = None,
    ) -> OrphanTurnRecord:
        return OrphanTurnRecord(
            turn_id=turn.turn_id,
            request_id=turn.request_id,
            category=category,
            reason_code=reason_code,
            run_id=run.run_id if run else None,
            result_id=result.result_id if result else None,
            result_status=result.status if result else None,
        )
