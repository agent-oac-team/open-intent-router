from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import CanonicalTurnModel, MemoryEventModel, TurnOutboxModel
from app.repositories.database import DatabaseResultRepository, DatabaseRunRepository
from app.repositories.json_utils import loads
from app.repositories.turns import DatabaseTurnRepository
from app.schemas.logs import AgentResult, AgentRun
from app.schemas.turns import CanonicalTurn, TurnUserInput
from app.services.orphan_turn_reconciler import (
    DatabaseOrphanTurnReconciler,
    OrphanTurnRepairConflict,
)


async def _seed_orphan(
    *,
    turn_repository,
    run_repository,
    result_repository,
    suffix: str,
    owner: tuple[str, str] = ("t1", "u1"),
    run_owner: tuple[str, str] | None = None,
    suppressed: bool = False,
    with_result: bool = True,
) -> None:
    tenant_id, user_id = owner
    run_tenant, run_user = run_owner or owner
    now = datetime(2026, 7, 17, tzinfo=UTC)
    await turn_repository.create_idempotent(
        CanonicalTurn(
            turn_id=f"turn_{suffix}",
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=f"session_{suffix}",
            request_id=f"request_{suffix}",
            source="host_chat",
            user_input=TurnUserInput(text=f"private-input-{suffix}"),
            created_at=now,
            updated_at=now,
        )
    )
    await run_repository.add_run(
        AgentRun(
            run_id=f"run_{suffix}",
            request_id=f"request_{suffix}",
            session_id=f"session_{suffix}",
            agent_id="agent_1",
            tenant_id=run_tenant,
            user_id=run_user,
            status="completed",
            invoker_type="mock",
            formation_suppressed=suppressed,
            input={"secret": f"run-secret-{suffix}"},
        )
    )
    if with_result:
        await result_repository.add_result(
            AgentResult(
                result_id=f"result_{suffix}",
                run_id=f"run_{suffix}",
                session_id=f"session_{suffix}",
                agent_id="agent_1",
                tenant_id=run_tenant,
                user_id=run_user,
                status="completed",
                message="completed",
                formation_suppressed=suppressed,
                output={"private": f"result-secret-{suffix}"},
            )
        )


async def test_orphan_scan_classifies_without_exposing_content(tmp_path, managed_database) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'orphan-scan.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    turns = DatabaseTurnRepository(session_factory)
    runs = DatabaseRunRepository(session_factory)
    results = DatabaseResultRepository(session_factory)
    await _seed_orphan(
        turn_repository=turns,
        run_repository=runs,
        result_repository=results,
        suffix="enabled",
    )
    await _seed_orphan(
        turn_repository=turns,
        run_repository=runs,
        result_repository=results,
        suffix="skipped",
        suppressed=True,
    )
    await _seed_orphan(
        turn_repository=turns,
        run_repository=runs,
        result_repository=results,
        suffix="ambiguous",
        with_result=False,
    )
    await _seed_orphan(
        turn_repository=turns,
        run_repository=runs,
        result_repository=results,
        suffix="conflict",
        run_owner=("t2", "u2"),
    )

    report = await DatabaseOrphanTurnReconciler(session_factory).scan(
        tenant_id="t1",
        user_id="u1",
        updated_before=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert report.counts == {
        "ambiguous": 1,
        "ownership_conflict": 1,
        "repairable_enabled": 1,
        "repairable_skipped": 1,
    }
    serialized = report.model_dump_json()
    assert "private-input" not in serialized
    assert "run-secret" not in serialized
    assert "result-secret" not in serialized


async def test_orphan_repair_is_scoped_atomic_and_idempotent(tmp_path, managed_database) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'orphan-repair.db'}",
    )
    await managed_database.initialize_schema(settings)
    session_factory = await managed_database.session_factory(settings)
    turns = DatabaseTurnRepository(session_factory)
    runs = DatabaseRunRepository(session_factory)
    results = DatabaseResultRepository(session_factory)
    for suffix, suppressed in (("enabled", False), ("skipped", True)):
        await _seed_orphan(
            turn_repository=turns,
            run_repository=runs,
            result_repository=results,
            suffix=suffix,
            suppressed=suppressed,
        )
    reconciler = DatabaseOrphanTurnReconciler(session_factory)

    enabled = await reconciler.repair(
        tenant_id="t1",
        user_id="u1",
        request_id="request_enabled",
        idempotency_key="repair-batch-1:request_enabled",
    )
    skipped = await reconciler.repair(
        tenant_id="t1",
        user_id="u1",
        request_id="request_skipped",
        idempotency_key="repair-batch-1:request_skipped",
    )
    replay = await reconciler.repair(
        tenant_id="t1",
        user_id="u1",
        request_id="request_enabled",
        idempotency_key="repair-batch-1:request_enabled",
    )

    assert enabled.action == "completed_with_outbox"
    assert skipped.action == "completed_skipped"
    assert replay.action == "idempotent_replay"
    assert replay.outbox_id == enabled.outbox_id
    async with session_factory() as session:
        turn_rows = (await session.execute(select(CanonicalTurnModel))).scalars().all()
        outboxes = (await session.execute(select(TurnOutboxModel))).scalars().all()
        events = (await session.execute(select(MemoryEventModel))).scalars().all()
    assert {row.status for row in turn_rows} == {"completed"}
    assert [row.turn_id for row in outboxes] == ["turn_enabled"]
    assert loads(outboxes[0].payload_text, {})["formation_eligibility"] == {
        "mode": "enforced",
        "execution_mode": "live",
        "suppressed": False,
        "reason_code": None,
        "policy_version": "orphan-reconciler-v1",
    }
    assert [event.turn_id for event in events] == ["turn_skipped"]
    assert loads(events[0].payload_text, {})["reason_code"] == "formation_suppressed"

    with pytest.raises(OrphanTurnRepairConflict, match="already terminal"):
        await reconciler.repair(
            tenant_id="t1",
            user_id="u1",
            request_id="request_enabled",
            idempotency_key="different-key",
        )
    with pytest.raises(OrphanTurnRepairConflict, match="not found"):
        await reconciler.repair(
            tenant_id="t2",
            user_id="u1",
            request_id="request_enabled",
            idempotency_key="cross-owner",
        )
