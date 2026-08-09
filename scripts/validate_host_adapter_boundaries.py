#!/usr/bin/env python3
"""Validate that generic OIR core code does not depend on host adapters."""

from __future__ import annotations

import ast
import re
from pathlib import Path

HOST_IMPORT_PREFIXES = ("host_adapters", "host_apps")
PROPRIETARY_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:oac|irs|coze|feishu|bot_id|route_path|allowed_user_tags)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def find_boundary_violations(repository_root: Path) -> list[str]:
    violations: list[str] = []
    core_files = sorted((repository_root / "app").rglob("*.py"))
    prompt_files = sorted((repository_root / "config" / "prompts").glob("*.y*ml"))

    for path in core_files:
        text = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(repository_root)
        violations.extend(_import_violations(text, relative_path))
        violations.extend(_identifier_violations(text, relative_path))

    for path in prompt_files:
        text = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(repository_root)
        violations.extend(_identifier_violations(text, relative_path))

    return violations


def _import_violations(source: str, path: Path) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: cannot inspect invalid Python syntax"]

    violations: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        for module in modules:
            if module in HOST_IMPORT_PREFIXES or module.startswith(
                tuple(f"{prefix}." for prefix in HOST_IMPORT_PREFIXES)
            ):
                violations.append(f"{path}:{node.lineno}: reverse host import: {module}")
    return violations


def _identifier_violations(source: str, path: Path) -> list[str]:
    violations: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        match = PROPRIETARY_IDENTIFIER.search(line)
        if match:
            violations.append(
                f"{path}:{line_number}: proprietary host identifier: {match.group(0)}"
            )
    return violations


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    violations = find_boundary_violations(repository_root)
    if violations:
        print("OIR Core / Host Adapter 边界校验失败：")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("OIR Core / Host Adapter 依赖方向与专有标识校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
