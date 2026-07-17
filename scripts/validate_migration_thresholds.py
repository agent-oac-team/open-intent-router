#!/usr/bin/env python3
"""校验迁移性能、Circuit、Diff 与稳定窗口阈值。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path("tests/contract/oac_irs/migration-records")


def main() -> None:
    thresholds = json.loads((ROOT / "acceptance-thresholds.json").read_text(encoding="utf-8"))
    baseline = json.loads((ROOT / thresholds["baseline_file"]).read_text(encoding="utf-8"))
    measurements = baseline.get("measurements", {})
    required_measurements = {
        "central_route_stub_llm",
        "knowledge_search_fake_vector",
        "knowledge_exact_read_memory",
    }
    if set(measurements) != required_measurements:
        raise ValueError("IRS 本地性能基线不完整")
    if any(
        item.get("samples", 0) < 100 or item.get("p95_ms", 0) <= 0 for item in measurements.values()
    ):
        raise ValueError("IRS 性能基线样本数或 p95 无效")

    circuit = thresholds["circuit_breaker"]
    if not 0 < circuit["failure_rate_to_open"] <= 1:
        raise ValueError("Circuit failure rate 无效")
    if circuit["minimum_requests"] > circuit["rolling_window_requests"]:
        raise ValueError("Circuit minimum requests 超过窗口")
    if circuit["successes_to_close"] > circuit["half_open_max_probes"]:
        raise ValueError("Circuit 无法在 probe 上限内关闭")

    diff = thresholds["diff_gates"]
    zero_gates = [key for key in diff if key.endswith("_count")]
    if any(diff[key] != 0 for key in zero_gates) or diff["replay_coverage"] != 1.0:
        raise ValueError("阻塞 Diff 或 replay coverage 门禁被放宽")
    stability = thresholds["stability_window"]
    allowed_keys = [key for key in stability if key.startswith("allowed_")]
    if any(stability[key] != 0 for key in allowed_keys):
        raise ValueError("稳定窗口错误预算必须为零")
    print("IRS 性能基线、Circuit、Diff 和稳定窗口阈值检查通过")


if __name__ == "__main__":
    main()
