import json
from argparse import Namespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from host_adapters.oac.cutover.gate import evaluate_cutover_gate
from scripts.manage_irs_cutover import inventory, run


def test_cutover_gate_is_fail_closed_and_requires_every_evidence_class() -> None:
    evidence = {
        "deployment_ready": True,
        "shadow_coverage": 1.0,
        "blocking_diff_count": 0,
        "state_rehearsal_passed": True,
        "primary_side_effect_count": 0,
        "fallback_drill_passed": True,
        "write_fence_passed": True,
        "irs_active_plan_count": 0,
        "irs_inflight_agent_count": 0,
        "irs_pending_callback_count": 0,
        "unhandled_late_callback_count": 0,
        "unknown_consumer_count": 0,
        "consumer_switch_passed": True,
        "smoke_passed": True,
        "oir_only_fact_source": True,
    }
    assert evaluate_cutover_gate(evidence).passed is True
    evidence["unknown_consumer_count"] = 1
    result = evaluate_cutover_gate(evidence)
    assert result.passed is False
    assert result.blocking_reasons == ["unknown_consumers_zero"]


async def test_irs_drain_inventory_and_explicit_termination(tmp_path) -> None:
    database = tmp_path / "irs.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE plans (plan_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE plan_steps (plan_id TEXT, step_id TEXT, agent_id TEXT, status TEXT)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE session_states (session_id TEXT PRIMARY KEY)"
        )
        await connection.exec_driver_sql("INSERT INTO plans VALUES ('secret-plan', 'running')")
        await connection.exec_driver_sql(
            "INSERT INTO plan_steps VALUES ('secret-plan','secret-step','secret-agent','running')"
        )
    async with engine.connect() as connection:
        snapshot = await inventory(connection)
    await engine.dispose()
    assert snapshot["active_plan_count"] == 1
    assert "secret-plan" not in json.dumps(snapshot)

    with pytest.raises(RuntimeError, match="requires"):
        await run(
            Namespace(
                database_url=f"sqlite+aiosqlite:///{database}",
                mode="terminate",
                confirm_terminate_irs_runtime=False,
            )
        )
    report = await run(
        Namespace(
            database_url=f"sqlite+aiosqlite:///{database}",
            mode="terminate",
            confirm_terminate_irs_runtime=True,
        )
    )
    assert report["drained"] is True
    assert report["after"]["active_plan_count"] == 0
    assert report["cutover_watermark"]
    assert report["historical_messages_retained"] is False
