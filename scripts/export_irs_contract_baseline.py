#!/usr/bin/env python3
"""导出 OAC 迁移范围内的 IRS 运行时 OpenAPI 契约快照。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

MIGRATION_OPERATIONS: tuple[tuple[str, str, str], ...] = (
    ("GET", "/api/v1/admin/agent-registry", "registry-list"),
    ("POST", "/api/v1/admin/agent-registry", "registry-create"),
    ("PUT", "/api/v1/admin/agent-registry/{agent_id}", "registry-update"),
    ("PATCH", "/api/v1/admin/agent-registry/{agent_id}/enabled", "registry-enabled"),
    ("DELETE", "/api/v1/admin/agent-registry/{agent_id}", "registry-delete"),
    ("POST", "/api/v1/central/route", "central-route"),
    ("POST", "/api/v1/central/events/navigation", "central-navigation-event"),
    ("POST", "/api/v1/central/events/agent", "central-agent-event"),
    ("POST", "/api/v1/central/plans/{plan_id}/confirm", "central-plan-confirm"),
    ("POST", "/api/v1/knowledge/search", "knowledge-search"),
    ("POST", "/api/v1/knowledge/grouped-search", "knowledge-grouped-search"),
    ("POST", "/api/v1/knowledge/read", "knowledge-read"),
    ("GET", "/api/v1/knowledge/assets", "knowledge-assets-list"),
    ("GET", "/api/v1/knowledge/assets/{asset_id}", "knowledge-asset-detail"),
    ("GET", "/api/v1/knowledge/assets/{asset_id}/chunks", "knowledge-asset-chunks"),
    ("GET", "/api/v1/knowledge/chunks/{chunk_id}", "knowledge-chunk-detail"),
    ("POST", "/api/v1/admin/knowledge/files", "knowledge-admin-upload"),
    ("GET", "/api/v1/admin/knowledge/files", "knowledge-admin-list"),
    ("GET", "/api/v1/admin/knowledge/files/{asset_id}", "knowledge-admin-detail"),
    ("GET", "/api/v1/admin/knowledge/files/{asset_id}/chunks", "knowledge-admin-chunks"),
    ("POST", "/api/v1/admin/knowledge/files/{asset_id}/retry", "knowledge-admin-retry"),
    ("DELETE", "/api/v1/admin/knowledge/files/{asset_id}", "knowledge-admin-delete"),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/contract/oac_irs/irs-baseline/v1/openapi"),
    )
    return parser.parse_args()


def _fetch_openapi(base_url: str) -> dict[str, Any]:
    with urlopen(f"{base_url.rstrip('/')}/openapi.json", timeout=10) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"IRS OpenAPI 返回 HTTP {response.status}")
        payload = json.load(response)
    if not isinstance(payload, dict) or "paths" not in payload:
        raise RuntimeError("IRS OpenAPI 响应不是有效的 OpenAPI 文档")
    return payload


def _schema_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            yield ref.rsplit("/", 1)[-1]
        for child in value.values():
            yield from _schema_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_refs(child)


def _referenced_schemas(operation: dict[str, Any], openapi: dict[str, Any]) -> dict[str, Any]:
    all_schemas = openapi.get("components", {}).get("schemas", {})
    pending = list(dict.fromkeys(_schema_refs(operation)))
    selected: dict[str, Any] = {}
    while pending:
        name = pending.pop(0)
        if name in selected:
            continue
        schema = all_schemas.get(name)
        if schema is None:
            raise RuntimeError(f"OpenAPI 引用了不存在的 Schema: {name}")
        selected[name] = schema
        pending.extend(ref for ref in _schema_refs(schema) if ref not in selected)
    return {name: selected[name] for name in sorted(selected)}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = _parse_args()
    openapi = _fetch_openapi(args.base_url)
    operation_snapshots: list[dict[str, str]] = []

    for method, path, fixture_name in MIGRATION_OPERATIONS:
        operation = openapi.get("paths", {}).get(path, {}).get(method.lower())
        if operation is None:
            raise RuntimeError(f"IRS OpenAPI 缺少迁移方法: {method} {path}")
        snapshot = {
            "openapi": openapi["openapi"],
            "info": openapi["info"],
            "method": method,
            "path": path,
            "operation": operation,
            "components": {"schemas": _referenced_schemas(operation, openapi)},
        }
        relative_path = Path("operations") / f"{fixture_name}.json"
        _write_json(args.output_dir / relative_path, snapshot)
        operation_snapshots.append(
            {
                "method": method,
                "path": path,
                "operation_id": operation.get("operationId", ""),
                "snapshot": relative_path.as_posix(),
            }
        )

    _write_json(args.output_dir / "openapi.json", openapi)
    _write_json(
        args.output_dir / "manifest.json",
        {
            "baseline": "irs-local-runtime-v1",
            "captured_at": datetime.now(UTC).isoformat(),
            "source": f"{args.base_url.rstrip('/')}/openapi.json",
            "service": openapi.get("info", {}),
            "operation_count": len(operation_snapshots),
            "operations": operation_snapshots,
            "excluded_surfaces": [
                "POST /api/v1/admin/agent-registry/sync-feishu",
                "POST /api/v1/agents/available",
                "GET /api/v1/sessions/{session_id}/chat-history",
            ],
        },
    )
    print(f"已导出 {len(operation_snapshots)} 个 IRS 迁移方法到 {args.output_dir}")


if __name__ == "__main__":
    main()
