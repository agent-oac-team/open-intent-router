import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.dependencies import get_registry_service  # noqa: E402
from app.schemas.agents import AgentDefinition  # noqa: E402
from app.schemas.registry_mutation import RegistryMutationCommand  # noqa: E402
from host_adapters.oac.authz import OAC_BUNDLE_CATALOG  # noqa: E402

BASELINE = {
    "strategy_analysis": (
        ("展业版", "运营版"),
        "12950a69672629371ee0bdcf6397d2bd3990c7b46d497be6325e4adf12f59527",
    ),
    "compliance_review": (
        ("展业版", "运营版"),
        "8604c022fbd0cd5a921c23a415918d23b618c636c2f488d3b67df55b4f0dc14c",
    ),
    "daily_wecom_task": (
        ("运营版",),
        "7b800713e148ddde2af985b8b0791b50ce456a450ebfff6b50f6b72a9c45206c",
    ),
    "marketing_poster": (
        ("运营版",),
        "70b4ddb567a868dec6f5df29e7eb839d8d3f5e4995c5cc8615d3ca2fee7cce44",
    ),
    "production_schedule": (
        ("运营版",),
        "3857f26352a0f7a0d4c3af05c48aa42128e805af8d9ea8ec0115bb8a6b85de57",
    ),
    "wecom_material": (
        ("展业版", "运营版"),
        "fee3ee2770d8a990b79aadc656181d0adc2f0d86d38005f130e8d0fc96655619",
    ),
    "feishu_labeling": (
        ("运营版",),
        "075d247919e019f7f23aa9badd49f2a53fe9a47454ec2e7cb2d0d5ae2b676cba",
    ),
    "bse_ipo": (
        ("展业版", "运营版"),
        "9d5c2846d7176286400c55a68d9427c5cb8f98b58c47e2673f3ccece3f31e8f7",
    ),
    "finance_digest": (
        ("展业版", "运营版"),
        "7e371c4c83a1592e82d489790fde6b124c222c10535ba8b464636e926d142734",
    ),
}
NON_PERMISSION_FIELDS = (
    "agent_id",
    "name",
    "description",
    "enabled",
    "type",
    "trigger",
    "invocation",
    "ui_handoff",
    "domain",
    "required_inputs",
    "optional_inputs",
    "output_schema",
)


@dataclass(frozen=True)
class MigrationItem:
    agent: AgentDefinition
    operation: str
    expected_tags: tuple[str, ...]
    expected_entitlements: tuple[str, ...]
    updated: AgentDefinition | None


def non_permission_hash(agent: AgentDefinition) -> str:
    payload = agent.model_dump(mode="json")
    stable = {field: payload[field] for field in NON_PERMISSION_FIELDS}
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_migration_plan(agents: list[AgentDefinition]) -> list[MigrationItem]:
    by_id = {agent.agent_id: agent for agent in agents}
    missing = sorted(set(BASELINE) - set(by_id))
    unexpected = sorted(set(by_id) - set(BASELINE))
    if missing or unexpected:
        raise ValueError(f"registry baseline mismatch: missing={missing}, unexpected={unexpected}")

    plan = []
    for agent_id, (tags, expected_hash) in BASELINE.items():
        agent = by_id[agent_id]
        if non_permission_hash(agent) != expected_hash:
            raise ValueError(f"non-permission baseline mismatch: {agent_id}")
        expected_entitlements = tuple(
            sorted(
                {
                    entitlement
                    for tag in tags
                    for entitlement in OAC_BUNDLE_CATALOG.entitlements_for_tag(tag)
                }
            )
        )
        projected_tags = tuple(
            bundle.legacy_tag
            for bundle in OAC_BUNDLE_CATALOG.bundles
            if any(grant in expected_entitlements for grant in bundle.grants)
        )
        groups = tuple(sorted(set(agent.access_policy.allow_groups)))
        entitlements = tuple(agent.access_policy.any_entitlements)
        if not groups and entitlements == expected_entitlements:
            plan.append(
                MigrationItem(agent, "unchanged", projected_tags, expected_entitlements, None)
            )
            continue
        if groups != tuple(sorted(tags)) or entitlements:
            raise ValueError(
                f"unsupported or mixed permission policy: {agent_id}: "
                f"groups={groups}, entitlements={entitlements}"
            )
        policy = agent.access_policy.model_copy(
            update={"allow_groups": [], "any_entitlements": list(expected_entitlements)}
        )
        plan.append(
            MigrationItem(
                agent,
                "update",
                projected_tags,
                expected_entitlements,
                agent.model_copy(update={"access_policy": policy}),
            )
        )
    return plan


def migration_report(plan: list[MigrationItem]) -> dict[str, Any]:
    return {
        "contract": "oac-agent-entitlement-migration/v1",
        "policy_version": OAC_BUNDLE_CATALOG.policy_version,
        "agent_count": len(plan),
        "changes": sum(item.operation == "update" for item in plan),
        "agents": [
            {
                "agent_id": item.agent.agent_id,
                "operation": item.operation,
                "before_groups": item.agent.access_policy.allow_groups,
                "after_entitlements": list(item.expected_entitlements),
                "compat_allowed_user_tags": list(item.expected_tags),
                "non_permission_hash": non_permission_hash(item.agent),
            }
            for item in plan
        ],
    }


async def apply_plan(service, plan: list[MigrationItem], *, actor_id: str) -> None:
    for item in plan:
        if item.updated is None:
            continue
        await service.mutate_definition(
            RegistryMutationCommand(
                operation="update",
                agent_id=item.agent.agent_id,
                actor_id=actor_id,
                source="oac_entitlement_migration_v1",
                expected_revision=item.agent.revision,
                definition=item.updated,
            )
        )


async def rollback_snapshot(service, snapshot: list[dict[str, Any]], *, actor_id: str) -> None:
    for payload in snapshot:
        previous = AgentDefinition.model_validate(payload)
        current = await service.get_definition(previous.agent_id)
        if current is None:
            raise ValueError(f"rollback Agent is missing: {previous.agent_id}")
        await service.mutate_definition(
            RegistryMutationCommand(
                operation="update",
                agent_id=previous.agent_id,
                actor_id=actor_id,
                source="oac_entitlement_migration_rollback_v1",
                expected_revision=current.revision,
                definition=previous,
            )
        )


async def run(args) -> dict[str, Any]:
    service = get_registry_service()
    await service.load()
    if args.rollback:
        snapshot = json.loads(args.rollback.read_text(encoding="utf-8"))
        await rollback_snapshot(service, snapshot["agents"], actor_id=args.actor_id)
        return {"rollback": "completed", "agent_count": len(snapshot["agents"])}

    plan = build_migration_plan(await service.list_definitions())
    report = migration_report(plan)
    if args.snapshot:
        args.snapshot.write_text(
            json.dumps(
                {
                    "contract": "oac-agent-entitlement-snapshot/v1",
                    "agents": [
                        item.agent.model_dump(mode="json")
                        for item in plan
                        if item.operation == "update"
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if args.apply:
        if args.snapshot is None:
            raise ValueError("--apply requires --snapshot")
        await apply_plan(service, plan, actor_id=args.actor_id)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--actor-id", default="migration-operator")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
