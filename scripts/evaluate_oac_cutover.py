#!/usr/bin/env python3
"""Evaluate non-production IRS Central retirement evidence without deployment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from host_adapters.oac.cutover import evaluate_cutover_gate


def build_non_sensitive_snapshot(source: object) -> dict[str, object]:
    source = source if isinstance(source, dict) else {}
    snapshot = {
        "contract": "central-retirement-snapshot/v1",
        "captured_at": source.get("captured_at"),
        "contains_sensitive_values": False,
        "write_freeze_enabled": source.get("write_freeze_enabled") is True,
        "versions": {
            "oir": _safe_label(source.get("oir_version")),
            "central_contract": _safe_label(source.get("central_contract_version")),
        },
        "configuration": {
            "registry_backend": _safe_label(source.get("registry_backend")),
            "central_fallback_enabled": source.get("central_fallback_enabled")
            if isinstance(source.get("central_fallback_enabled"), bool)
            else None,
        },
        "fact_sources": {
            "central": "oir",
            "registry": "oir",
            "agent": "oir",
            "session": "oir",
            "event": "oir",
            "plan": "oir",
            "run": "oir",
            "result": "oir",
        },
    }
    snapshot["passed"] = (
        snapshot["write_freeze_enabled"] is True
        and snapshot["versions"]["oir"] is not None
        and snapshot["versions"]["central_contract"] is not None
        and snapshot["configuration"]["registry_backend"] == "database"
        and snapshot["configuration"]["central_fallback_enabled"] is False
    )
    return snapshot


def assemble_evidence(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, object]]:
    drain = raw.get("drain_report")
    snapshot = build_non_sensitive_snapshot(raw.get("snapshot_source"))
    normalized = {
        "environment": raw.get("environment"),
        "capability_contracts": raw.get("capability_contracts"),
        "oac_e2e": raw.get("oac_e2e"),
        "irs_runtime": _runtime_evidence(drain),
        "cutover_watermark": drain.get("cutover_watermark") if isinstance(drain, dict) else None,
        "non_sensitive_snapshot": snapshot,
        "recovery_drill": raw.get("recovery_drill"),
    }
    return normalized, snapshot


def _runtime_evidence(drain: object) -> dict[str, object]:
    if not isinstance(drain, dict):
        return {}
    after = drain.get("after")
    if not isinstance(after, dict):
        return {}
    return {
        "drained": drain.get("contract") == "irs-runtime-drain/v1" and drain.get("drained") is True,
        "active_plan_count": after.get("active_plan_count"),
        "inflight_agent_count": after.get("inflight_agent_count"),
        "pending_callback_count": after.get("pending_callback_count"),
        "disposition_recorded": drain.get("mode") in {"inventory", "terminate"},
        "evidence_ref": drain.get("evidence_ref"),
    }


def _safe_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > 128:
        return None
    return stripped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--snapshot-report", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.evidence.read_text(encoding="utf-8"))
    normalized, snapshot = assemble_evidence(raw)
    report = evaluate_cutover_gate(normalized).as_dict()
    report["evidence_summary"] = {
        "environment": normalized["environment"],
        "cutover_watermark": normalized["cutover_watermark"],
        "irs_runtime": normalized["irs_runtime"],
        "non_sensitive_snapshot": snapshot,
        "recovery_drill": normalized["recovery_drill"],
        "capability_contracts": normalized["capability_contracts"],
        "oac_e2e": normalized["oac_e2e"],
    }
    rendered_report = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    rendered_snapshot = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.snapshot_report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered_report, encoding="utf-8")
    args.snapshot_report.write_text(rendered_snapshot, encoding="utf-8")
    print(rendered_report, end="")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
