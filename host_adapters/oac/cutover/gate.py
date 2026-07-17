from dataclasses import dataclass


@dataclass(frozen=True)
class CutoverGateResult:
    passed: bool
    checks: dict[str, bool]
    blocking_reasons: list[str]

    def as_dict(self) -> dict:
        return {
            "contract": "oac-oir-cutover-gate/v1",
            "passed": self.passed,
            "checks": self.checks,
            "blocking_reasons": self.blocking_reasons,
        }


def evaluate_cutover_gate(evidence: dict) -> CutoverGateResult:
    checks = {
        "deployment_ready": evidence.get("deployment_ready") is True,
        "shadow_coverage_100": evidence.get("shadow_coverage") == 1.0,
        "blocking_diffs_zero": evidence.get("blocking_diff_count") == 0,
        "state_rehearsal_passed": evidence.get("state_rehearsal_passed") is True,
        "primary_side_effects_zero": evidence.get("primary_side_effect_count") == 0,
        "circuit_and_fallback_drill_passed": evidence.get("fallback_drill_passed") is True,
        "write_fence_passed": evidence.get("write_fence_passed") is True,
        "irs_active_plans_zero": evidence.get("irs_active_plan_count") == 0,
        "irs_inflight_agents_zero": evidence.get("irs_inflight_agent_count") == 0,
        "irs_pending_callbacks_zero": evidence.get("irs_pending_callback_count") == 0,
        "late_callbacks_disposed": evidence.get("unhandled_late_callback_count") == 0,
        "unknown_consumers_zero": evidence.get("unknown_consumer_count") == 0,
        "oac_and_coze_switched": evidence.get("consumer_switch_passed") is True,
        "contract_and_transport_smoke_passed": evidence.get("smoke_passed") is True,
        "oir_only_fact_source": evidence.get("oir_only_fact_source") is True,
    }
    blocking = [name for name, passed in checks.items() if not passed]
    return CutoverGateResult(passed=not blocking, checks=checks, blocking_reasons=blocking)
