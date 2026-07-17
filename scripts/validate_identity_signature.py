#!/usr/bin/env python3
"""验证 OIR Host HMAC 契约测试向量和关键拒绝条件。"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

CONTRACT = Path("tests/contract/oac_irs/identity/v1/contract.json")


def _signature(secret: str, canonical: str) -> str:
    digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _timestamp_allowed(now: int, timestamp: int, max_clock_skew_seconds: int) -> bool:
    return abs(now - timestamp) <= max_clock_skew_seconds


def _consume_nonce(cache: set[tuple[str, str]], key_id: str, nonce: str) -> bool:
    key = (key_id, nonce)
    if key in cache:
        return False
    cache.add(key)
    return True


def main() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    vector = contract["test_vector"]
    body_hash = hashlib.sha256(vector["request"]["body"].encode()).hexdigest()
    if body_hash != vector["content_sha256"]:
        raise ValueError("body SHA-256 与测试向量不一致")
    actual = _signature(vector["secret"], vector["canonical"])
    if not hmac.compare_digest(actual, vector["signature"]):
        raise ValueError("HMAC 测试向量不一致")
    tampered = vector["canonical"].replace("\n42\n", "\n43\n")
    if hmac.compare_digest(_signature(vector["secret"], tampered), vector["signature"]):
        raise ValueError("篡改 user ID 未导致签名失败")

    policy = contract["policy"]
    timestamp = vector["identity"]["timestamp"]
    if _timestamp_allowed(
        timestamp + policy["max_clock_skew_seconds"] + 1,
        timestamp,
        policy["max_clock_skew_seconds"],
    ):
        raise ValueError("过期 timestamp 测试未被拒绝")
    nonce_cache: set[tuple[str, str]] = set()
    if not _consume_nonce(nonce_cache, vector["identity"]["key_id"], vector["identity"]["nonce"]):
        raise ValueError("首次 nonce 被错误拒绝")
    if _consume_nonce(nonce_cache, vector["identity"]["key_id"], vector["identity"]["nonce"]):
        raise ValueError("nonce 防重放测试失败")

    print("Python HMAC 向量、身份篡改、时间窗和 nonce 重放检查通过")


if __name__ == "__main__":
    main()
