#!/usr/bin/env python3
"""Reject hidden database-engine creation outside explicit ownership scopes."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

SCANNED_DIRECTORIES = ("app", "host_adapters", "host_apps", "scripts", "tests")
LEGACY_ENTRYPOINTS = frozenset({"create_engine", "create_session_factory", "create_all_tables"})
MANAGED_ENGINE_SCOPE = ("app/db/managed.py", "ManagedDatabase.__aenter__")
RAW_ENGINE_SCOPE = ("tests/support/database.py", "raw_engine_scope")
POSTGRESQL_MIGRATION_SCOPE = (
    "tests/test_memory_postgresql_integration.py",
    "test_real_postgresql_legacy_memory_migration_is_rollback_safe_and_idempotent",
)
ENGINE_FACTORY_IMPORT_PATHS = frozenset(
    {
        MANAGED_ENGINE_SCOPE[0],
        RAW_ENGINE_SCOPE[0],
        POSTGRESQL_MIGRATION_SCOPE[0],
    }
)
ENGINE_FACTORY_NAMES = frozenset({"create_async_engine", "_create_async_engine"})


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if not isinstance(node, ast.Attribute):
        return None
    parent = _qualified_name(node.value)
    return f"{parent}.{node.attr}" if parent else None


class _OwnershipInspector(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.violations: list[str] = []
        self._engine_factory_names = set(ENGINE_FACTORY_NAMES)

    @property
    def current_scope(self) -> str:
        return ".".join(self.scope)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name in LEGACY_ENTRYPOINTS:
            self.violations.append(f"{self.relative_path}:{node.lineno}: {node.name}")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "app.db.session":
            for imported in node.names:
                if imported.name in LEGACY_ENTRYPOINTS:
                    self.violations.append(
                        f"{self.relative_path}:{node.lineno}: {imported.name} import"
                    )
        for imported in node.names:
            if imported.name not in ENGINE_FACTORY_NAMES:
                continue
            if node.module == "sqlalchemy.ext.asyncio":
                self._engine_factory_names.add(imported.asname or imported.name)
                if self.relative_path in ENGINE_FACTORY_IMPORT_PATHS:
                    continue
            self.violations.append(
                f"{self.relative_path}:{node.lineno}: "
                "create_async_engine import outside an ownership scope"
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _qualified_name(node.func)
        terminal_name = name.rsplit(".", 1)[-1] if name else None
        if terminal_name in LEGACY_ENTRYPOINTS:
            self.violations.append(f"{self.relative_path}:{node.lineno}: {terminal_name} call")
        if terminal_name in self._engine_factory_names and not self._is_allowed_engine_scope():
            self.violations.append(
                f"{self.relative_path}:{node.lineno}: create_async_engine outside an ownership scope"
            )
        self.generic_visit(node)

    def _is_allowed_engine_scope(self) -> bool:
        current = (self.relative_path, self.current_scope)
        return current in {
            MANAGED_ENGINE_SCOPE,
            RAW_ENGINE_SCOPE,
            POSTGRESQL_MIGRATION_SCOPE,
        }


def _python_sources(root: Path) -> Iterable[Path]:
    for directory in SCANNED_DIRECTORIES:
        yield from sorted((root / directory).rglob("*.py"))


def unexpected_database_entrypoints(root: Path) -> tuple[str, ...]:
    """Return direct or legacy Engine entry points outside named lexical scopes."""

    violations: list[str] = []
    for source in _python_sources(root):
        relative_path = source.relative_to(root).as_posix()
        inspector = _OwnershipInspector(relative_path)
        inspector.visit(ast.parse(source.read_text(encoding="utf-8"), filename=str(source)))
        violations.extend(inspector.violations)
    return tuple(sorted(violations))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = unexpected_database_entrypoints(root)
    if not violations:
        return 0
    print("Database ownership boundaries were bypassed:")
    print("\n".join(violations))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
