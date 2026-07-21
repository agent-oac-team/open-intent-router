from pathlib import Path

import pytest

from scripts.import_irs_agent_registry import load_irs_agents
from scripts.migrate_oac_agent_entitlements import (
    BASELINE,
    build_migration_plan,
    migration_report,
    non_permission_hash,
)

SOURCE = Path("/Users/lijingtong/project/intent_recon_sys/sql/agent_registry.csv")


def _legacy_agents():
    agents = load_irs_agents(SOURCE)
    tags_by_id = {agent_id: tags for agent_id, (tags, _) in BASELINE.items()}
    return [
        agent.model_copy(
            update={
                "access_policy": agent.access_policy.model_copy(
                    update={
                        "allow_groups": list(tags_by_id[agent.agent_id]),
                        "any_entitlements": [],
                    }
                )
            }
        )
        for agent in agents
    ]


def test_migration_baseline_has_nine_stable_agents_and_hashes() -> None:
    agents = _legacy_agents()
    assert len(agents) == len(BASELINE) == 9
    assert {agent.agent_id for agent in agents} == set(BASELINE)
    assert all(non_permission_hash(agent) == BASELINE[agent.agent_id][1] for agent in agents)


def test_migration_maps_single_and_dual_version_agents_and_is_idempotent() -> None:
    plan = build_migration_plan(_legacy_agents())
    production = next(item for item in plan if item.agent.agent_id == "production_schedule")
    strategy = next(item for item in plan if item.agent.agent_id == "strategy_analysis")
    assert production.expected_entitlements == ("workspace.operations.access",)
    assert strategy.expected_entitlements == (
        "workspace.operations.access",
        "workspace.sales_enablement.access",
    )
    assert strategy.expected_tags == ("运营版", "展业版")
    migrated = [item.updated for item in plan]
    rerun = build_migration_plan(migrated)
    assert all(item.operation == "unchanged" for item in rerun)
    report = migration_report(plan)
    assert report["agent_count"] == 9 and report["changes"] == 9


@pytest.mark.parametrize("mode", ["unknown", "empty", "mixed"])
def test_migration_rejects_unknown_empty_or_mixed_policy_before_writes(mode: str) -> None:
    agents = _legacy_agents()
    first = agents[0]
    if mode == "unknown":
        policy = first.access_policy.model_copy(update={"allow_groups": ["未知版"]})
    elif mode == "empty":
        policy = first.access_policy.model_copy(update={"allow_groups": []})
    else:
        policy = first.access_policy.model_copy(
            update={"any_entitlements": ["workspace.operations.access"]}
        )
    agents[0] = first.model_copy(update={"access_policy": policy})
    with pytest.raises(ValueError, match="unsupported or mixed"):
        build_migration_plan(agents)
