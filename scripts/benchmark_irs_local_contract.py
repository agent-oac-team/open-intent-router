#!/usr/bin/env python3
"""测量 IRS 确定性 Route/Search/Read Handler 本地延迟基线。"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--irs-root", type=Path, default=Path("../intent_recon_sys"))
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/contract/oac_irs/migration-records/irs-local-performance.json"),
    )
    return parser.parse_args()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _measure(call: Callable[[int], Any], *, warmup: int, samples: int) -> dict[str, float | int]:
    for index in range(warmup):
        response = call(-index - 1)
        if response.status_code != 200:
            raise RuntimeError(f"warmup HTTP {response.status_code}: {response.text}")
    elapsed_ms: list[float] = []
    for index in range(samples):
        started = time.perf_counter_ns()
        response = call(index)
        elapsed_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        if response.status_code != 200:
            raise RuntimeError(f"sample HTTP {response.status_code}: {response.text}")
    return {
        "samples": samples,
        "min_ms": round(min(elapsed_ms), 3),
        "p50_ms": round(_percentile(elapsed_ms, 0.50), 3),
        "p95_ms": round(_percentile(elapsed_ms, 0.95), 3),
        "p99_ms": round(_percentile(elapsed_ms, 0.99), 3),
        "max_ms": round(max(elapsed_ms), 3),
    }


def main() -> None:  # noqa: C901, PLR0915
    args = _parse_args()
    irs_root = args.irs_root.resolve()
    if not (irs_root / "app" / "main.py").exists():
        raise RuntimeError(f"无效 IRS 根目录: {irs_root}")
    sys.path.insert(0, str(irs_root))

    from app.config import get_settings
    from fastapi.testclient import TestClient
    from tests.test_knowledge_generic_access_api import (
        _asset,
        _chunk,
        _index_chunk,
        _seed_dependencies,
    )

    from app.dependencies import get_llm_router_service
    from app.main import app
    from app.schemas.knowledge import KnowledgeSourceRef
    from tests.test_api import override_router_service

    measurements: dict[str, dict[str, float | int]] = {}

    route_settings = get_settings().model_copy(
        update={"knowledge_enabled": False, "knowledge_central_policy_enabled": False}
    )
    app.dependency_overrides[get_settings] = lambda: route_settings
    app.dependency_overrides[get_llm_router_service] = override_router_service
    try:
        with TestClient(app) as client:

            def call_route(index: int):
                return client.post(
                    "/api/v1/central/route",
                    json={
                        "request_id": f"req_benchmark_route_{index}",
                        "session_id": f"sess_benchmark_route_{index}",
                        "user_id": "user_benchmark",
                        "user_tags": ["运营版"],
                        "source": "central_chat",
                        "user_query": "帮我写一条企微文案",
                    },
                )

            measurements["central_route_stub_llm"] = _measure(
                call_route, warmup=args.warmup, samples=args.samples
            )
    finally:
        app.dependency_overrides.clear()

    repository, embedding, vector = _seed_dependencies()
    asset = _asset("asset_benchmark", business_domain="内容生产")
    chunk = _chunk(
        "chunk_benchmark",
        asset.asset_id,
        "财富节节高达标额三千万元及以上奖励一万零八百元微信立减金。",
        business_domain="内容生产",
        source_ref=KnowledgeSourceRef(source_type="excel", sheet="04 活动表", row=24),
    )
    _index_chunk(repository, embedding, vector, asset=asset, chunk=chunk)
    try:
        with TestClient(app) as client:

            def call_search(index: int):
                return client.post(
                    "/api/v1/knowledge/search",
                    json={
                        "request_id": f"req_benchmark_search_{index}",
                        "session_id": "sess_benchmark_knowledge",
                        "user_id": "user_benchmark",
                        "user_tags": ["运营版"],
                        "consumer": "central",
                        "consumer_id": "benchmark",
                        "purpose": "answer",
                        "query": "财富节节高奖励",
                        "scope": {"asset_ids": [asset.asset_id]},
                        "top_k": 5,
                    },
                )

            def call_read(index: int):
                return client.post(
                    "/api/v1/knowledge/read",
                    json={
                        "request_id": f"req_benchmark_read_{index}",
                        "session_id": "sess_benchmark_knowledge",
                        "user_id": "user_benchmark",
                        "consumer": "central",
                        "consumer_id": "benchmark",
                        "purpose": "answer",
                        "target": {"type": "asset", "asset_id": asset.asset_id},
                    },
                )

            measurements["knowledge_search_fake_vector"] = _measure(
                call_search, warmup=args.warmup, samples=args.samples
            )
            measurements["knowledge_exact_read_memory"] = _measure(
                call_read, warmup=args.warmup, samples=args.samples
            )
    finally:
        app.dependency_overrides.clear()

    output = {
        "baseline": "irs-local-deterministic-v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "source_revision": "348cf93fa3441108481631b7768d5ab7670c706e",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "external_providers": "IRS repository fakes",
        },
        "measurements": measurements,
        "scope_note": "用于迁移回归和相对 Diff，不是生产 SLA。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(measurements, ensure_ascii=False))


if __name__ == "__main__":
    main()
