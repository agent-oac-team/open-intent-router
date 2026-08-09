#!/usr/bin/env python3
"""校验 OAC 中控时序 fixture 的可执行结构和代理路径映射。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ALLOWED_KINDS = {"http", "external", "assert_state", "include"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fixture",
        type=Path,
        nargs="?",
        default=Path("tests/contract/oac_irs/oac-sequences/v1/sequences.json"),
    )
    return parser.parse_args()


def _expected_target(public_path: str) -> str:
    prefix = "/api/central"
    if not public_path.startswith(prefix + "/"):
        raise ValueError(f"HTTP step 不是 OAC Central 公开路径: {public_path}")
    rest = public_path.removeprefix(prefix)
    if rest.startswith(("/agents/", "/sessions/", "/knowledge/")):
        return "/api/v1" + rest
    return "/api/v1/central" + rest


def _validate_http_step(sequence_id: str, step: dict[str, Any]) -> None:
    request = step.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"{sequence_id}/{step.get('id')} 缺少 request")
    if request.get("method") not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError(f"{sequence_id}/{step.get('id')} method 非法")
    public_path = request.get("public_path")
    target_path = request.get("target_path")
    if not isinstance(public_path, str) or not isinstance(target_path, str):
        raise ValueError(f"{sequence_id}/{step.get('id')} 缺少 public_path/target_path")
    expected = _expected_target(public_path)
    if target_path != expected:
        raise ValueError(
            f"{sequence_id}/{step.get('id')} target_path={target_path}，期望 {expected}"
        )
    expected_status = step.get("expect", {}).get("status")
    if not isinstance(expected_status, int):
        raise ValueError(f"{sequence_id}/{step.get('id')} 缺少预期 HTTP status")


def main() -> None:
    args = _parse_args()
    payload = json.loads(args.fixture.read_text(encoding="utf-8"))
    if payload.get("protocol") != "oac-host-sequence/v1":
        raise ValueError("不支持的时序 fixture protocol")
    sequences = payload.get("sequences")
    if not isinstance(sequences, list) or not sequences:
        raise ValueError("sequences 必须是非空数组")
    sequence_ids = {item.get("id") for item in sequences}
    if len(sequence_ids) != len(sequences) or None in sequence_ids:
        raise ValueError("sequence id 必须存在且唯一")

    steps_by_sequence = {
        sequence["id"]: {step.get("id") for step in sequence.get("steps", [])}
        for sequence in sequences
    }
    for sequence in sequences:
        sequence_id = sequence["id"]
        steps = sequence.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{sequence_id} 缺少 steps")
        step_ids = {step.get("id") for step in steps}
        if len(step_ids) != len(steps) or None in step_ids:
            raise ValueError(f"{sequence_id} step id 必须存在且唯一")
        for step in steps:
            kind = step.get("kind")
            if kind not in ALLOWED_KINDS:
                raise ValueError(f"{sequence_id}/{step.get('id')} kind 非法: {kind}")
            if kind == "http":
                _validate_http_step(sequence_id, step)
            if kind == "include":
                included_sequence = step.get("sequence")
                if included_sequence not in sequence_ids:
                    raise ValueError(f"{sequence_id}/{step.get('id')} 引用了未知 sequence")
                unknown_steps = set(step.get("steps", [])) - steps_by_sequence[included_sequence]
                if unknown_steps:
                    raise ValueError(
                        f"{sequence_id}/{step.get('id')} 引用了未知 step: {sorted(unknown_steps)}"
                    )

    print(f"已校验 {len(sequences)} 个 OAC 可执行中控时序 fixture")


if __name__ == "__main__":
    main()
