#!/usr/bin/env python3
"""校验 execution_ticket 字段位置和向后兼容约束。"""

from __future__ import annotations

import json
from pathlib import Path

CONTRACT_V1 = Path("tests/contract/oac_irs/execution-ticket/v1/contract.json")
CONTRACT_V2 = Path("tests/contract/oac_irs/execution-ticket/v2/contract.json")


def main() -> None:
    contract = json.loads(CONTRACT_V1.read_text(encoding="utf-8"))
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

    conditional = json.loads(CONTRACT_V2.read_text(encoding="utf-8"))
    if conditional.get("extends") != contract["contract"]:
        raise ValueError("Ticket v2 必须显式扩展冻结的 v1 契约")
    expected_version = conditional["request"]["expected_state_version"]
    if not expected_version["required_for_conditional_plan_completion"]:
        raise ValueError("条件 Plan 完成必须携带 expected_state_version")
    if expected_version["required_for_legacy_clients"]:
        raise ValueError("expected_state_version 不得成为旧客户端必填字段")
    replay_names = {item["name"] for item in conditional["replay_cases"]}
    if replay_names != {"current", "stale_before_claim", "racing_after_check"}:
        raise ValueError("条件 Plan 完成 replay 场景不完整")
    security = conditional["security_inheritance"]
    required_security = {
        "body_signature_covers_new_fields",
        "bad_key_rejected",
        "wrong_profile_rejected",
        "tampered_body_rejected",
        "expired_request_rejected",
        "replayed_nonce_rejected",
        "cross_owner_plan_hidden",
        "submission_unknown_fails_closed",
        "ticket_disclosure_forbidden",
    }
    if any(not security.get(key) for key in required_security):
        raise ValueError("条件 Plan 完成未继承完整 Host V2 安全约束")
    print("execution_ticket v1 兼容与 v2 条件 Plan 完成契约检查通过")


if __name__ == "__main__":
    main()
