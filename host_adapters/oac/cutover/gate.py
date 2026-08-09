from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

CENTRAL_CAPABILITY_MAPPING: dict[str, dict[str, object]] = {
    "central": {
        "legacy_surface": [
            "POST /api/v1/central/route",
            "POST /api/v1/central/events/navigation",
            "POST /api/v1/central/events/agent",
            "GET /api/v1/central/active-plan",
            "POST /api/v1/central/plans/{plan_id}/confirm",
        ],
        "oir_owner": "Routing / Canonical Turn / application ports",
        "verification": [
            ".venv/bin/python -m pytest tests/test_oac_central_compat.py tests/test_oac_central_handlers.py"
        ],
    },
    "registry": {
        "legacy_surface": ["Agent Registry list/create/update/enable/delete"],
        "oir_owner": "Agent Registry",
        "verification": [
            ".venv/bin/python -m pytest tests/test_oac_registry_compat.py tests/test_registry_atomic_mutation.py tests/test_registry_revision_audit.py"
        ],
    },
    "agent": {
        "legacy_surface": ["open_agent / continue_agent / exit_agent"],
        "oir_owner": "Route Decision / Agent Session / UI Handoff",
        "verification": [
            ".venv/bin/python -m pytest tests/test_oac_central_handlers.py::test_central_route_e2e_covers_all_legacy_actions tests/test_oac_agent_entitlement_routing_e2e.py"
        ],
    },
    "session": {
        "legacy_surface": ["session context / history reads"],
        "oir_owner": "Session / Message read models",
        "verification": [
            ".venv/bin/python -m pytest tests/test_api.py tests/test_oac_central_compat.py::test_route_request_mapper_uses_trusted_identity_and_keeps_history_as_context"
        ],
    },
    "event": {
        "legacy_surface": ["navigation event / agent event callback"],
        "oir_owner": "Agent Event / Execution Trace",
        "verification": [
            ".venv/bin/python -m pytest tests/test_oac_central_handlers.py::test_navigation_and_plan_confirm_keep_legacy_status_and_shape tests/test_oac_central_handlers.py::test_route_issues_ticket_and_completed_event_consumes_it"
        ],
    },
    "plan": {
        "legacy_surface": ["active plan / plan confirm"],
        "oir_owner": "Plan / Plan Step",
        "verification": [
            ".venv/bin/python -m pytest tests/test_oac_central_handlers.py::test_navigation_and_plan_confirm_keep_legacy_status_and_shape tests/test_plan_confirm_concurrency.py tests/test_plan_repository_ownership.py"
        ],
    },
    "run": {
        "legacy_surface": ["host-executed agent lifecycle"],
        "oir_owner": "Delegated Run / Execution Ticket",
        "verification": [
            ".venv/bin/python -m pytest tests/test_delegated_run_contracts.py tests/test_delegated_run_start.py tests/test_delegated_run_progress.py tests/test_delegated_run_failure.py tests/test_delegated_run_maintenance.py"
        ],
    },
    "result": {
        "legacy_surface": ["agent result callback / history projection"],
        "oir_owner": "Agent Result / Canonical Turn completion",
        "verification": [
            ".venv/bin/python -m pytest tests/test_delegated_run_completion.py tests/test_turn_transaction_coordinator.py tests/test_canonical_invocation_store.py"
        ],
    },
}

REQUIRED_OAC_E2E_FLOWS = (
    "route",
    "agent_switch",
    "agent_exit",
    "plan_confirm",
    "agent_callback",
    "history_read",
)

REQUIRED_RECOVERY_CHECKS = (
    "oir_write_freeze_verified",
    "data_reconciliation_passed",
    "ambiguous_writes_not_replayed",
    "incomplete_runs_disposed",
    "irs_not_post_retirement_rollback",
)


@dataclass(frozen=True)
class CutoverGateResult:
    passed: bool
    decision: str
    deletion_allowed: bool
    checks: dict[str, bool]
    blocking_reasons: list[str]
    capability_mapping: dict[str, dict[str, object]]
    exclusions: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "contract": "oir-central-retirement-gate/v1",
            "passed": self.passed,
            "decision": self.decision,
            "deletion_allowed": self.deletion_allowed,
            "checks": self.checks,
            "blocking_reasons": self.blocking_reasons,
            "capability_mapping": self.capability_mapping,
            "exclusions": self.exclusions,
        }


def evaluate_cutover_gate(evidence: dict[str, Any]) -> CutoverGateResult:
    runtime = evidence.get("irs_runtime")
    snapshot = evidence.get("non_sensitive_snapshot")
    recovery = evidence.get("recovery_drill")
    checks = {
        "non_production_evidence": evidence.get("environment") in {"local", "test", "rehearsal"},
        "capability_contracts_complete": _all_evidence_passed(
            evidence.get("capability_contracts"), tuple(CENTRAL_CAPABILITY_MAPPING)
        ),
        "oac_e2e_complete": _all_evidence_passed(evidence.get("oac_e2e"), REQUIRED_OAC_E2E_FLOWS),
        "irs_runtime_drained_or_disposed": _runtime_drained(runtime),
        "cutover_watermark_recorded": _valid_watermark(evidence.get("cutover_watermark")),
        "non_sensitive_snapshot_recorded": _snapshot_safe(snapshot),
        "recovery_and_reconciliation_drill_passed": _all_evidence_passed(
            recovery, REQUIRED_RECOVERY_CHECKS
        ),
    }
    blocking = [name for name, passed in checks.items() if not passed]
    passed = not blocking
    return CutoverGateResult(
        passed=passed,
        decision="go" if passed else "no-go",
        deletion_allowed=passed,
        checks=checks,
        blocking_reasons=blocking,
        capability_mapping=CENTRAL_CAPABILITY_MAPPING,
        exclusions={
            "unknown_consumer_enumeration_required": False,
            "central_zero_traffic_observation_days": 0,
        },
    )


def _all_evidence_passed(value: object, required: tuple[str, ...]) -> bool:
    if not isinstance(value, dict) or set(value) != set(required):
        return False
    return all(
        isinstance(value[name], dict)
        and value[name].get("passed") is True
        and isinstance(value[name].get("evidence_ref"), str)
        and bool(value[name]["evidence_ref"].strip())
        for name in required
    )


def _runtime_drained(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("drained") is True
        and value.get("active_plan_count") == 0
        and value.get("inflight_agent_count") == 0
        and value.get("pending_callback_count") == 0
        and value.get("disposition_recorded") is True
        and isinstance(value.get("evidence_ref"), str)
        and bool(value["evidence_ref"].strip())
    )


def _valid_watermark(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _snapshot_safe(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("contract") == "central-retirement-snapshot/v1"
        and value.get("passed") is True
        and value.get("contains_sensitive_values") is False
        and value.get("write_freeze_enabled") is True
        and _valid_watermark(value.get("captured_at"))
        and value.get("fact_sources")
        == {capability: "oir" for capability in CENTRAL_CAPABILITY_MAPPING}
    )
