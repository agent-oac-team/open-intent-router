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
    memory_by_agent = {row["agent_id"]: row["memory_context"] for row in report["agents"]}
    assert memory_by_agent["strategy_analysis"] == {
        "mode": "prefetch",
        "scopes": ["user_preference", "stable_fact"],
        "max_items": 5,
        "controlled_retrieval": None,
        "metadata": {},
    }


def test_strategy_analysis_is_migrated_to_the_analysis_ui_handoff() -> None:
    agents = {agent.agent_id: agent for agent in load_irs_agents(SOURCE)}
    strategy = agents["strategy_analysis"]

    assert strategy.type == "ui_handoff"
    assert strategy.invocation.type == "ui_handoff"
    assert strategy.invocation.provider_config == {}
    assert strategy.ui_handoff.mode == "route"
    assert strategy.ui_handoff.route == "/analysis"


def test_initial_memory_rollout_is_explicit_for_exactly_two_agents() -> None:
    agents = {agent.agent_id: agent for agent in load_irs_agents(SOURCE)}
    enabled = {
        agent_id: agent.context.memory
        for agent_id, agent in agents.items()
        if agent.context.memory.mode == "prefetch"
    }

    assert set(enabled) == {"strategy_analysis", "compliance_review"}
    for memory in enabled.values():
        assert memory.scopes == ["user_preference", "stable_fact"]
        assert memory.max_items == 5
    assert all(
        agent.context.memory.mode == "disabled"
        for agent_id, agent in agents.items()
        if agent_id not in enabled
    )
