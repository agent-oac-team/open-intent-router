#!/usr/bin/env python3
"""生成或校验 OAC/IRS 迁移契约基线的 SHA-256 索引。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("tests/contract/oac_irs")
INDEX_NAME = "baseline-index.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def _artifacts(root: Path) -> list[dict[str, Any]]:
    artifacts = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == INDEX_NAME:
            continue
        payload = path.read_bytes()
        artifacts.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        )
    return artifacts


def _counts(root: Path) -> dict[str, int]:
    route = json.loads((root / "golden/route/v1/dataset.json").read_text(encoding="utf-8"))
    knowledge = json.loads((root / "golden/knowledge/v1/dataset.json").read_text(encoding="utf-8"))
    sequences = json.loads((root / "oac-sequences/v1/sequences.json").read_text(encoding="utf-8"))
    return {
        "openapi_operations": len(
            list((root / "irs-baseline/v1/openapi/operations").glob("*.json"))
        ),
        "success_fixtures": len(list((root / "irs-baseline/v1/success").glob("*.json"))),
        "error_fixtures": len(list((root / "irs-baseline/v1/errors").glob("*.json"))),
        "oac_sequences": len(sequences["sequences"]),
        "route_golden_cases": len(route["cases"]),
        "knowledge_golden_cases": len(knowledge["cases"]),
    }


def main() -> None:
    args = _parse_args()
    root = args.root.resolve()
    index_path = root / INDEX_NAME
    artifacts = _artifacts(root)
    counts = _counts(root)
    expected_counts = {
        "openapi_operations": 22,
        "success_fixtures": 22,
        "error_fixtures": 9,
        "oac_sequences": 6,
        "route_golden_cases": 10,
        "knowledge_golden_cases": 17,
    }
    if counts != expected_counts:
        raise ValueError(f"契约基线数量不一致: {counts}")

    if args.check:
        current = json.loads(index_path.read_text(encoding="utf-8"))
        if current.get("artifacts") != artifacts or current.get("counts") != counts:
            raise ValueError("契约基线索引已过期，请重新生成")
        print(f"契约基线索引校验通过，共 {len(artifacts)} 个文件")
        return

    index = {
        "contract": "oac-irs-migration-baseline/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "irs_source_revision": "348cf93fa3441108481631b7768d5ab7670c706e",
        "oac_source_revision": "cd97c1b with working-tree evidence at 2026-07-16",
        "counts": counts,
        "artifacts": artifacts,
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成契约基线索引，共 {len(artifacts)} 个文件")


if __name__ == "__main__":
    main()
