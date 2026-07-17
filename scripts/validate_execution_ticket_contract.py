#!/usr/bin/env python3
"""校验 execution_ticket 字段位置和向后兼容约束。"""

from __future__ import annotations

import json
from pathlib import Path

CONTRACT = Path("tests/contract/oac_irs/execution-ticket/v1/contract.json")


def main() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    positions = contract["wire_positions"]
    for name in ("central_route_response", "agent_event_request"):
        position = positions[name]
        if position["json_path"] != "$.execution_ticket":
            raise ValueError(f"{name} 必须使用顶层 execution_ticket")
        if position["required"]:
            raise ValueError(f"{name} execution_ticket 不得成为必填字段")
    forbidden = set(contract["transport_rules"]["body_fallback_location_forbidden"])
    if forbidden != {"frontend_context", "output", "metadata"}:
        raise ValueError("Ticket 禁止位置约束不完整")
    compatibility = contract["compatibility"]
    required_compatibility = {
        "old_route_response_without_ticket_valid",
        "old_agent_event_without_ticket_valid",
        "ticketed_write_to_legacy_irs_forbidden",
    }
    if any(not compatibility.get(key) for key in required_compatibility):
        raise ValueError("Ticket 向后兼容或写栅栏约束缺失")
    for example in contract["examples"].values():
        if example.get("execution_ticket") != "<opaque-ticket>":
            raise ValueError("示例不得包含真实或可解析 Ticket")
    print("execution_ticket 顶层可选字段、生命周期和兼容约束检查通过")


if __name__ == "__main__":
    main()
