from pathlib import Path

from scripts.import_irs_agent_registry import (
    build_reconciliation_report,
    load_irs_agents,
)

SOURCE = Path("/Users/lijingtong/project/intent_recon_sys/sql/agent_registry.csv")


def test_loads_all_nine_irs_agents_with_stable_ids_and_mappings() -> None:
    agents = load_irs_agents(SOURCE)
    report = build_reconciliation_report(agents)

    assert len(agents) == 9
    assert len({agent.agent_id for agent in agents}) == 9
    assert report["passed"] is True
    assert report["failed"] == []
    assert all(agent.access_policy.allow_tenants == ["oac"] for agent in agents)
    assert all(
        agent.invocation.provider_config.get("bot_id") or agent.ui_handoff.route for agent in agents
    )


def test_empty_route_provider_agent_does_not_gain_fake_ui_handoff() -> None:
    agents = {agent.agent_id: agent for agent in load_irs_agents(SOURCE)}
    strategy = agents["strategy_analysis"]

    assert strategy.invocation.provider_config["bot_id"] == "7613696818723848192"
    assert strategy.ui_handoff.mode == "none"
    assert strategy.ui_handoff.route is None
