import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.dependencies import get_registry_service  # noqa: E402
from app.schemas.agents import AgentDefinition, SchemaContract  # noqa: E402
from host_adapters.oac.mappers.registry import (  # noqa: E402
    registry_agent_from_native,
    registry_agent_to_native,
)
from host_adapters.oac.schemas.registry import RegistryAgent  # noqa: E402

DEFAULT_SOURCE = Path("/Users/lijingtong/project/intent_recon_sys/sql/agent_registry.csv")


def load_irs_agents(path: Path) -> list[AgentDefinition]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    agents = [_row_to_agent(row) for row in rows]
    ids = [agent.agent_id for agent in agents]
    if len(ids) != len(set(ids)):
        raise ValueError("IRS Agent Registry contains duplicate agent_id values")
    return agents


async def import_agents(agents: list[AgentDefinition], *, dry_run: bool) -> list[dict[str, Any]]:
    service = get_registry_service()
    results = []
    for agent in agents:
        existing = await service.get_definition(agent.agent_id)
        expected_revision = existing.revision if existing else 0
        saved = (
            agent
            if dry_run
            else await service.upsert_definition(agent, expected_revision=expected_revision)
        )
        results.append(
            {
                "agent_id": agent.agent_id,
                "operation": "unchanged"
                if existing == agent
                else "update"
                if existing
                else "create",
                "revision": saved.revision,
                "enabled": saved.enabled,
            }
        )
    return results


def build_reconciliation_report(agents: list[AgentDefinition]) -> dict[str, Any]:
    rows = []
    for agent in agents:
        compat = registry_agent_from_native(agent)
        rows.append(
            {
                "agent_id": agent.agent_id,
                "name": agent.name,
                "enabled": agent.enabled,
                "permission_groups": compat.allowed_user_tags,
                "permission_entitlements": agent.access_policy.any_entitlements,
                "positive_triggers": agent.trigger.positive_examples,
                "negative_triggers": agent.trigger.negative_examples,
                "invocation_type": agent.invocation.type,
                "bot_id_present": bool(agent.invocation.provider_config.get("bot_id")),
                "ui_handoff_route": agent.ui_handoff.route,
                "checks": {
                    "stable_id": True,
                    "name": bool(agent.name),
                    "permission": bool(agent.access_policy.any_entitlements),
                    "positive_trigger": bool(agent.trigger.positive_examples),
                    "invocation": bool(
                        agent.invocation.provider_config.get("bot_id") or agent.ui_handoff.route
                    ),
                    "ui_handoff": (
                        agent.ui_handoff.route is not None if agent.type == "ui_handoff" else True
                    ),
                },
            }
        )
    failed = [
        {"agent_id": row["agent_id"], "checks": row["checks"]}
        for row in rows
        if not all(row["checks"].values())
    ]
    return {
        "contract": "oir-agent-registry-reconciliation/v1",
        "expected_count": 9,
        "actual_count": len(rows),
        "passed": len(rows) == 9 and not failed,
        "failed": failed,
        "agents": rows,
    }


def _row_to_agent(row: dict[str, str]) -> AgentDefinition:
    legacy = RegistryAgent(
        agent_id=row["agent_id"].strip(),
        name=row["name"].strip(),
        description=row["description"].strip(),
        bot_id=row.get("bot_id", "").strip(),
        route_path=row.get("route_path", "").strip(),
        allowed_user_tags=_json_list(row.get("allowed_user_tags")),
        positive_keywords=_json_list(row.get("positive_keywords")),
        negative_keywords=_json_list(row.get("negative_keywords")),
        enabled=row.get("enabled", "").strip().lower() in {"t", "true", "1", "yes"},
    )
    agent = registry_agent_to_native(legacy)
    output_fields = _json_list(row.get("output_schema"))
    return agent.model_copy(
        update={
            "domain": row.get("domain", "").strip() or None,
            "required_inputs": _json_list(row.get("required_inputs")),
            "optional_inputs": _json_list(row.get("optional_inputs")),
            "output_schema": SchemaContract(properties={field: {} for field in output_fields}),
        }
    )


def _json_list(value: str | None) -> list[str]:
    parsed = json.loads(value or "[]")
    if not isinstance(parsed, list):
        raise ValueError("Registry list field must contain a JSON array")
    return [str(item).strip() for item in parsed if str(item).strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    agents = load_irs_agents(args.source)
    report = build_reconciliation_report(agents)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    results = asyncio.run(import_agents(agents, dry_run=args.dry_run))
    print(json.dumps({"report": report, "imports": results}, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
