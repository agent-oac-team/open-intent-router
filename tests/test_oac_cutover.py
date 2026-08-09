import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from host_adapters.oac.cutover import (
    CENTRAL_CAPABILITY_MAPPING,
    REQUIRED_OAC_E2E_FLOWS,
    REQUIRED_RECOVERY_CHECKS,
    evaluate_cutover_gate,
)
from scripts.evaluate_oac_cutover import build_non_sensitive_snapshot
from scripts.manage_irs_cutover import inventory, run


def _passing_gate_evidence() -> dict:
    return {
        "environment": "test",
        "capability_contracts": {
            capability: {"passed": True, "evidence_ref": f"pytest::{capability}"}
            for capability in CENTRAL_CAPABILITY_MAPPING
        },
        "oac_e2e": {
            flow: {"passed": True, "evidence_ref": f"pytest::{flow}"}
            for flow in REQUIRED_OAC_E2E_FLOWS
        },
        "irs_runtime": {
            "drained": True,
            "active_plan_count": 0,
            "inflight_agent_count": 0,
            "pending_callback_count": 0,
            "disposition_recorded": True,
            "evidence_ref": "pytest::test_irs_drain_inventory_and_explicit_termination",
        },
        "cutover_watermark": "2026-07-31T00:00:00+00:00",
        "non_sensitive_snapshot": {
            "contract": "central-retirement-snapshot/v1",
            "passed": True,
            "contains_sensitive_values": False,
            "write_freeze_enabled": True,
            "captured_at": "2026-07-31T00:01:00+00:00",
            "fact_sources": {capability: "oir" for capability in CENTRAL_CAPABILITY_MAPPING},
        },
        "recovery_drill": {
            check: {"passed": True, "evidence_ref": f"pytest::{check}"}
            for check in REQUIRED_RECOVERY_CHECKS
        },
        # Diagnostic only: neither value may become a retirement prerequisite.
        "unknown_consumer_count": 17,
        "central_zero_traffic_observation_days": 0,
    }


def test_central_retirement_gate_passes_without_unknown_consumer_enumeration_or_zero_traffic() -> (
    None
):
    result = evaluate_cutover_gate(_passing_gate_evidence())

    assert result.passed is True
    assert result.decision == "go"
    assert result.deletion_allowed is True
    assert result.exclusions == {
        "unknown_consumer_enumeration_required": False,
        "central_zero_traffic_observation_days": 0,
    }
    assert result.as_dict()["contract"] == "oir-central-retirement-gate/v1"


def test_capability_mapping_has_executable_verification_for_every_required_domain() -> None:
    assert set(CENTRAL_CAPABILITY_MAPPING) == {
        "central",
        "registry",
        "agent",
        "session",
        "event",
        "plan",
        "run",
        "result",
    }
    for mapping in CENTRAL_CAPABILITY_MAPPING.values():
        assert mapping["legacy_surface"]
        assert mapping["oir_owner"]
        assert mapping["verification"]
        assert all(
            command.startswith(".venv/bin/python -m pytest ") for command in mapping["verification"]
        )


def test_central_retirement_gate_is_fail_closed_for_missing_capability_evidence() -> None:
    evidence = _passing_gate_evidence()
    del evidence["capability_contracts"]["session"]
    result = evaluate_cutover_gate(evidence)

    assert result.passed is False
    assert result.decision == "no-go"
    assert result.deletion_allowed is False
    assert result.blocking_reasons == ["capability_contracts_complete"]


def test_central_retirement_gate_requires_reviewable_drain_evidence() -> None:
    evidence = _passing_gate_evidence()
    del evidence["irs_runtime"]["evidence_ref"]

    result = evaluate_cutover_gate(evidence)

    assert result.passed is False
    assert result.blocking_reasons == ["irs_runtime_drained_or_disposed"]


def test_central_retirement_gate_rejects_production_evidence() -> None:
    evidence = _passing_gate_evidence()
    evidence["environment"] = "production"

    result = evaluate_cutover_gate(evidence)

    assert result.passed is False
    assert "non_production_evidence" in result.blocking_reasons


def test_non_sensitive_snapshot_fails_closed_when_fallback_state_is_missing() -> None:
    snapshot = build_non_sensitive_snapshot(
        {
            "captured_at": "2026-07-31T00:01:00+00:00",
            "write_freeze_enabled": True,
            "oir_version": "test-sha",
            "central_contract_version": "v1",
            "registry_backend": "database",
        }
    )

    assert snapshot["passed"] is False


def test_central_retirement_cli_builds_allowlisted_snapshot_and_go_report(tmp_path) -> None:
    evidence = _passing_gate_evidence()
    evidence["drain_report"] = {
        "contract": "irs-runtime-drain/v1",
        "mode": "inventory",
        "drained": True,
        "after": {
            "active_plan_count": 0,
            "inflight_agent_count": 0,
            "pending_callback_count": 0,
        },
        "cutover_watermark": evidence.pop("cutover_watermark"),
        "evidence_ref": "pytest::test_irs_drain_inventory_and_explicit_termination",
    }
    evidence.pop("irs_runtime")
    evidence["snapshot_source"] = {
        "captured_at": "2026-07-31T00:01:00+00:00",
        "write_freeze_enabled": True,
        "oir_version": "test-sha",
        "central_contract_version": "v1",
        "registry_backend": "database",
        "central_fallback_enabled": False,
        "database_url": "postgresql://must-not-appear",
        "admin_token": "must-not-appear",
    }
    evidence.pop("non_sensitive_snapshot")
    evidence_path = tmp_path / "evidence.json"
    report_path = tmp_path / "gate.json"
    snapshot_path = tmp_path / "snapshot.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_oac_cutover.py",
            "--evidence",
            str(evidence_path),
            "--report",
            str(report_path),
            "--snapshot-report",
            str(snapshot_path),
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONPATH": "."},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    snapshot_text = snapshot_path.read_text(encoding="utf-8")
    assert report["decision"] == "go"
    assert report["deletion_allowed"] is True
    assert set(report["capability_mapping"]) == set(CENTRAL_CAPABILITY_MAPPING)
    assert "postgresql://" not in snapshot_text
    assert "must-not-appear" not in snapshot_text


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
