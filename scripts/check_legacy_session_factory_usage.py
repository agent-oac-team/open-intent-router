"""Temporary expand-phase gate for hidden Session Factory ownership.

The list is intentionally exact and may only shrink while Tickets #64 and #65
migrate callers.  Ticket #66 removes both this checker and the legacy factory.
"""

from __future__ import annotations

import ast
from pathlib import Path

ALLOWED_CALL_SITES = frozenset(
    {
        "app/dependencies.py:218",
        "app/dependencies.py:253",
        "app/dependencies.py:268",
        "app/dependencies.py:295",
        "app/dependencies.py:328",
        "app/dependencies.py:536",
        "app/dependencies.py:545",
        "app/dependencies.py:559",
        "app/dependencies.py:567",
        "app/dependencies.py:626",
        "app/dependencies.py:637",
        "app/dependencies.py:701",
        "app/dependencies.py:730",
        "app/services/registry_service.py:178",
        "host_apps/oac/dependencies.py:206",
        "scripts/reconcile_orphan_turns.py:29",
        "scripts/smoke_mem0_memory_loop.py:78",
        "tests/test_canonical_invocation_store.py:47",
        "tests/test_database_migrations.py:483",
        "tests/test_database_migrations.py:512",
        "tests/test_database_migrations.py:587",
        "tests/test_delegated_run_cancel.py:78",
        "tests/test_delegated_run_completion.py:56",
        "tests/test_delegated_run_completion.py:307",
        "tests/test_delegated_run_failure.py:47",
        "tests/test_delegated_run_maintenance.py:71",
        "tests/test_delegated_run_persistence.py:25",
        "tests/test_delegated_run_progress.py:46",
        "tests/test_delegated_run_progress.py:221",
        "tests/test_delegated_run_start.py:36",
        "tests/test_delegated_run_timeout_runtime.py:53",
        "tests/test_direct_binding_resolution.py:615",
        "tests/test_events_plans_evidence.py:516",
        "tests/test_events_plans_evidence.py:579",
        "tests/test_execution_tickets.py:34",
        "tests/test_execution_tickets.py:190",
        "tests/test_execution_tickets.py:257",
        "tests/test_execution_tickets.py:331",
        "tests/test_execution_tickets.py:385",
        "tests/test_execution_trace.py:233",
        "tests/test_external_execution.py:725",
        "tests/test_external_execution.py:726",
        "tests/test_memory_debug_management_metrics.py:925",
        "tests/test_memory_debug_management_metrics.py:1369",
        "tests/test_memory_debug_management_metrics.py:1411",
        "tests/test_memory_debug_management_metrics.py:1964",
        "tests/test_memory_debug_management_metrics.py:2004",
        "tests/test_memory_debug_management_metrics.py:2107",
        "tests/test_memory_debug_management_metrics.py:2211",
        "tests/test_memory_debug_management_metrics.py:2256",
        "tests/test_memory_debug_management_metrics.py:2258",
        "tests/test_memory_debug_management_metrics.py:2259",
        "tests/test_memory_debug_management_metrics.py:2341",
        "tests/test_memory_debug_management_metrics.py:2393",
        "tests/test_memory_debug_management_metrics.py:2395",
        "tests/test_memory_debug_management_metrics.py:2396",
        "tests/test_memory_debug_management_metrics.py:2447",
        "tests/test_memory_debug_management_metrics.py:2499",
        "tests/test_memory_debug_management_metrics.py:2500",
        "tests/test_memory_debug_management_metrics.py:2529",
        "tests/test_memory_event_repository_filters.py:21",
        "tests/test_memory_event_repository_filters.py:75",
        "tests/test_memory_formation_repositories.py:345",
        "tests/test_memory_formation_repositories.py:383",
        "tests/test_memory_formation_repositories.py:439",
        "tests/test_memory_formation_repositories.py:473",
        "tests/test_memory_formation_repositories.py:502",
        "tests/test_memory_formation_repositories.py:544",
        "tests/test_memory_formation_repositories.py:566",
        "tests/test_memory_index_operation_repositories.py:36",
        "tests/test_memory_index_operation_repositories.py:81",
        "tests/test_memory_index_operation_repositories.py:118",
        "tests/test_memory_index_operation_repositories.py:142",
        "tests/test_memory_indexing.py:1001",
        "tests/test_memory_indexing.py:1027",
        "tests/test_memory_indexing.py:1084",
        "tests/test_memory_indexing.py:1136",
        "tests/test_memory_invocation_plan_integration.py:371",
        "tests/test_memory_invocation_plan_integration.py:387",
        "tests/test_memory_invocation_plan_integration.py:1040",
        "tests/test_memory_invocation_plan_integration.py:1133",
        "tests/test_memory_invocation_plan_integration.py:1526",
        "tests/test_memory_item_repositories.py:37",
        "tests/test_memory_item_repositories.py:128",
        "tests/test_memory_lifecycle_service.py:82",
        "tests/test_memory_postgresql_integration.py:321",
        "tests/test_memory_postgresql_integration.py:500",
        "tests/test_memory_postgresql_integration.py:501",
        "tests/test_memory_postgresql_integration.py:698",
        "tests/test_memory_revision_repositories.py:101",
        "tests/test_memory_revision_repositories.py:150",
        "tests/test_memory_trace_repositories.py:62",
        "tests/test_memory_trace_repositories.py:147",
        "tests/test_memory_trace_repositories.py:200",
        "tests/test_memory_trigger_runtime.py:74",
        "tests/test_memory_trigger_runtime.py:248",
        "tests/test_memory_trigger_runtime.py:249",
        "tests/test_native_definition_migration.py:59",
        "tests/test_native_resource_ownership.py:576",
        "tests/test_orphan_turn_reconciler.py:84",
        "tests/test_orphan_turn_reconciler.py:140",
        "tests/test_plan_binding_revalidation.py:1694",
        "tests/test_plan_cancellation_truth.py:218",
        "tests/test_plan_cancellation_truth.py:529",
        "tests/test_plan_repository_ownership.py:168",
        "tests/test_plan_repository_ownership.py:194",
        "tests/test_plan_repository_ownership.py:227",
        "tests/test_plan_repository_ownership.py:255",
        "tests/test_plan_repository_ownership.py:307",
        "tests/test_plan_repository_ownership.py:353",
        "tests/test_registry_atomic_mutation.py:51",
        "tests/test_registry_atomic_mutation.py:96",
        "tests/test_registry_atomic_mutation.py:117",
        "tests/test_registry_revision_audit.py:50",
        "tests/test_router_context_assembly.py:143",
        "tests/test_turn_outbox_memory_formation.py:227",
        "tests/test_turn_outbox_repositories.py:29",
        "tests/test_turn_repositories.py:43",
        "tests/test_turn_service.py:25",
        "tests/test_turn_transaction_coordinator.py:58",
        "tests/test_turn_transaction_coordinator.py:254",
        "tests/test_turn_transaction_coordinator.py:313",
    }
)


def find_call_sites(root: Path) -> frozenset[str]:
    call_sites: set[str] = set()
    for path in sorted(
        source
        for directory in ("app", "host_adapters", "host_apps", "scripts", "tests")
        for source in (root / directory).rglob("*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        direct_names, module_aliases = _factory_bindings(tree)
        if not direct_names and not module_aliases:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_factory_call(
                node.func,
                direct_names=direct_names,
                module_aliases=module_aliases,
            ):
                call_sites.add(f"{path.relative_to(root)}:{node.lineno}")
    return frozenset(call_sites)


def _factory_bindings(tree: ast.AST) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    names: set[str] = set()
    module_aliases: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "app.db.session" and item.asname:
                    module_aliases[item.asname] = ("app", "db", "session")
                elif item.name == "app.db" and item.asname:
                    module_aliases[item.asname] = ("app", "db")
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == "app.db.session":
            for item in node.names:
                if item.name in {"create_session_factory", "*"}:
                    names.add(item.asname or "create_session_factory")
        elif node.module == "app.db":
            for item in node.names:
                if item.name == "session":
                    module_aliases[item.asname or item.name] = ("app", "db", "session")
        elif node.module == "app":
            for item in node.names:
                if item.name == "db":
                    module_aliases[item.asname or item.name] = ("app", "db")
    return frozenset(names), module_aliases


def _is_factory_call(
    function: ast.expr,
    *,
    direct_names: frozenset[str],
    module_aliases: dict[str, tuple[str, ...]],
) -> bool:
    if isinstance(function, ast.Name):
        return function.id in direct_names
    qualified_name = _qualified_name(function)
    if qualified_name is None:
        return False
    if qualified_name[0] in module_aliases:
        qualified_name = module_aliases[qualified_name[0]] + qualified_name[1:]
    return qualified_name == ("app", "db", "session", "create_session_factory")


def _qualified_name(node: ast.expr) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if not isinstance(node, ast.Attribute):
        return None
    parent = _qualified_name(node.value)
    if parent is None:
        return None
    return parent + (node.attr,)


def unexpected_call_sites(root: Path) -> frozenset[str]:
    return find_call_sites(root) - ALLOWED_CALL_SITES


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    unexpected = sorted(unexpected_call_sites(root))
    if not unexpected:
        return 0
    print("New legacy Session Factory call sites are not permitted:")
    print("\n".join(unexpected))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
