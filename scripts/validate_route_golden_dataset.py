#!/usr/bin/env python3
"""校验 Route Golden Dataset 的枚举和验收维度覆盖。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROUTE_ACTIONS = {
    "reply",
    "clarify",
    "open_agent",
    "continue_agent",
    "exit_agent",
    "show_plan",
    "unsupported",
    "silent",
}
REQUIRED_DIMENSIONS = {
    "single_intent",
    "multi_intent",
    "permission_allow",
    "permission_deny",
    "clarification",
    "unsupported",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dataset",
        type=Path,
        nargs="?",
        default=Path("tests/contract/oac_irs/golden/route/v1/dataset.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    if payload.get("version") != "route-golden/v1":
        raise ValueError("Route Golden Dataset 版本错误")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases 必须是非空数组")
    case_ids = [case.get("id") for case in cases]
    if len(case_ids) != len(set(case_ids)) or None in case_ids:
        raise ValueError("case id 必须存在且唯一")

    actions = {case.get("expected", {}).get("action") for case in cases}
    if actions != ROUTE_ACTIONS:
        raise ValueError(
            f"8 种 Route action 覆盖不完整，missing={sorted(ROUTE_ACTIONS - actions)}，"
            f"extra={sorted(actions - ROUTE_ACTIONS)}"
        )
    dimensions = {dimension for case in cases for dimension in case.get("dimensions", [])}
    missing_dimensions = REQUIRED_DIMENSIONS - dimensions
    if missing_dimensions:
        raise ValueError(f"Route 验收维度缺失: {sorted(missing_dimensions)}")

    for case in cases:
        request = case.get("input", {})
        expected = case.get("expected", {})
        for field in ("request_id", "session_id", "user_id", "user_tags", "source"):
            if field not in request:
                raise ValueError(f"{case['id']} 缺少 input.{field}")
        if "permission_outcome" not in expected:
            raise ValueError(f"{case['id']} 缺少 permission_outcome")
        if expected.get("action") == "show_plan" and not isinstance(expected.get("plan"), dict):
            raise ValueError(f"{case['id']} show_plan 必须定义 Plan 结构断言")

    print(f"已校验 {len(cases)} 个 Route Golden case，覆盖全部 8 种 action")


if __name__ == "__main__":
    main()
