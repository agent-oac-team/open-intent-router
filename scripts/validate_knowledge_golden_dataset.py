#!/usr/bin/env python3
"""校验 Knowledge Golden Dataset 的接口、权限和稳定资产覆盖。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_OPERATIONS = {
    "search",
    "grouped_search",
    "read",
    "assets_list",
    "asset_detail",
    "asset_chunks",
    "chunk_detail",
}
REQUIRED_DIMENSIONS = {"permission", "no_match", "empty_03"}
EXPECTED_ASSET_KEYS = ["01", "02", "03", "04", "05", "06"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dataset",
        type=Path,
        nargs="?",
        default=Path("tests/contract/oac_irs/golden/knowledge/v1/dataset.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    if payload.get("version") != "knowledge-golden/v1":
        raise ValueError("Knowledge Golden Dataset 版本错误")
    assets = payload.get("assets", {})
    if list(assets) != EXPECTED_ASSET_KEYS:
        raise ValueError(f"稳定资产键错误: {list(assets)}")
    if assets["03"].get("expected_data_rows") != 0 or assets["03"].get("status") != "deferred":
        raise ValueError("03 客群表必须是零数据行的 deferred 资产")

    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases 必须是非空数组")
    case_ids = [case.get("id") for case in cases]
    if len(case_ids) != len(set(case_ids)) or None in case_ids:
        raise ValueError("case id 必须存在且唯一")
    operations = {case.get("operation") for case in cases}
    missing_operations = REQUIRED_OPERATIONS - operations
    if missing_operations:
        raise ValueError(f"Knowledge 操作覆盖缺失: {sorted(missing_operations)}")
    dimensions = {dimension for case in cases for dimension in case.get("dimensions", [])}
    missing_dimensions = REQUIRED_DIMENSIONS - dimensions
    if missing_dimensions:
        raise ValueError(f"Knowledge 验收维度缺失: {sorted(missing_dimensions)}")

    grouped_empty = next(
        (case for case in cases if case.get("id") == "grouped-all-assets-including-empty-03"),
        None,
    )
    if (
        grouped_empty is None
        or grouped_empty.get("expected", {}).get("asset_keys_in_order") != EXPECTED_ASSET_KEYS
    ):
        raise ValueError("Grouped Search 未固定 01-06 和空 03 槽位")
    permission_cases = [case for case in cases if "permission" in case.get("dimensions", [])]
    if not permission_cases or any(
        case.get("expected", {}).get("evidence_count") != 0 for case in permission_cases
    ):
        raise ValueError("权限负向 case 必须禁止返回 evidence")

    print(
        f"已校验 {len(cases)} 个 Knowledge Golden case，覆盖 "
        f"{len(REQUIRED_OPERATIONS)} 类查询/读取操作"
    )


if __name__ == "__main__":
    main()
