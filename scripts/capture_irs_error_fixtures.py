#!/usr/bin/env python3
"""抓取 IRS 迁移契约中的错误、降级和 warning fixture。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--irs-root", type=Path, default=Path("../intent_recon_sys"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/contract/oac_irs/irs-baseline/v1/errors"),
    )
    return parser.parse_args()


def _record(
    output_dir: Path,
    name: str,
    response: Any,
    *,
    method: str,
    path: str,
    request_body: Any = None,
    request_headers: dict[str, str] | None = None,
    expected_status: int,
    expected_warning: str | None = None,
) -> None:
    if response.status_code != expected_status:
        raise RuntimeError(
            f"{name} 期望 HTTP {expected_status}，实际 {response.status_code}: {response.text}"
        )
    body = response.json() if response.content else None
    if expected_warning:
        warning_codes = {
            warning.get("code")
            for warning in (body or {}).get("warnings", [])
            if isinstance(warning, dict)
        }
        if expected_warning not in warning_codes:
            raise RuntimeError(f"{name} 缺少 warning={expected_warning}: {body}")
    headers = {
        key: "<redacted>" if "token" in key.lower() or "authorization" in key.lower() else value
        for key, value in (request_headers or {}).items()
    }
    payload = {
        "name": name,
        "request": {
            "method": method,
            "path": path,
            "headers": headers,
            "body": request_body,
        },
        "response": {
            "status": response.status_code,
            "headers": {"content-type": response.headers.get("content-type", "")},
            "body": body,
        },
        "assertions": {
            "expected_status": expected_status,
            "expected_warning": expected_warning,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:  # noqa: C901, PLR0915
    args = _parse_args()
    irs_root = args.irs_root.resolve()
    if not (irs_root / "app" / "main.py").exists():
        raise RuntimeError(f"无效 IRS 根目录: {irs_root}")
    sys.path.insert(0, str(irs_root))

    from app.config import get_settings
    from app.integrations.knowledge_embedding import FakeKnowledgeEmbeddingClient
    from app.integrations.milvus_vector_index import FakeKnowledgeVectorIndexClient
    from fastapi.testclient import TestClient
    from tests.test_admin_api import StubRepository
    from tests.test_knowledge_generic_access_api import (
        _asset,
        _chunk,
        _index_chunk,
        _seed_dependencies,
    )
    from tests.test_knowledge_search_gaps import (
        DIM,
        _base_payload,
        _seed_search_dependencies,
        _SlowFakeVectorIndexClient,
    )

    from app.dependencies import (
        get_agent_registry_sync_repository,
    )
    from app.main import app
    from app.schemas.knowledge import KnowledgeAsset, KnowledgeChunk, KnowledgeSourceRef

    output_dir = args.output_dir.resolve()

    # 422 request validation.
    invalid_route = {"request_id": "req_invalid"}
    with TestClient(app) as client:
        response = client.post("/api/v1/central/route", json=invalid_route)
    _record(
        output_dir,
        "central-validation-error",
        response,
        method="POST",
        path="/api/v1/central/route",
        request_body=invalid_route,
        expected_status=422,
    )

    # 401 authorization failures for both control surfaces.
    auth_settings = get_settings().model_copy(update={"admin_sync_token": "fixture-secret"})
    app.dependency_overrides[get_settings] = lambda: auth_settings
    app.dependency_overrides[get_agent_registry_sync_repository] = StubRepository
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/admin/agent-registry")
            _record(
                output_dir,
                "registry-unauthorized",
                response,
                method="GET",
                path="/api/v1/admin/agent-registry",
                expected_status=401,
            )
            response = client.get("/api/v1/admin/knowledge/files")
            _record(
                output_dir,
                "knowledge-admin-unauthorized",
                response,
                method="GET",
                path="/api/v1/admin/knowledge/files",
                expected_status=401,
            )
    finally:
        app.dependency_overrides.clear()

    # Normal no-match is HTTP 200 with an explicit warning.
    _seed_search_dependencies()
    no_match_body = _base_payload(
        request_id="req_fixture_no_match",
        query="完全不存在的契约测试知识",
    )
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/knowledge/search", json=no_match_body)
        _record(
            output_dir,
            "knowledge-no-match",
            response,
            method="POST",
            path="/api/v1/knowledge/search",
            request_body=no_match_body,
            expected_status=200,
            expected_warning="no_match",
        )
    finally:
        app.dependency_overrides.clear()

    # Permission filtering remains a successful transport response.
    repository, embedding, vector = _seed_search_dependencies()
    restricted_asset = KnowledgeAsset(
        asset_id="asset_fixture_restricted",
        source_type="document",
        source_name="受限契约文档",
        business_domain="合规",
    )
    restricted_chunk = KnowledgeChunk(
        chunk_id="chunk_fixture_restricted",
        asset_id=restricted_asset.asset_id,
        content="仅允许中控读取的受限契约内容。",
        source_ref=KnowledgeSourceRef(source_type="document", document_name="受限契约文档"),
        business_domain="合规",
        sensitivity="restricted",
        content_hash="hash_fixture_restricted",
    )
    _index_chunk(
        repository,
        embedding,
        vector,
        asset=restricted_asset,
        chunk=restricted_chunk,
    )
    permission_body = _base_payload(
        request_id="req_fixture_permission",
        consumer="agent",
        consumer_id="fixture_agent",
        purpose="execute",
        query="受限契约内容",
    )
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/knowledge/search", json=permission_body)
        _record(
            output_dir,
            "knowledge-permission-filtered",
            response,
            method="POST",
            path="/api/v1/knowledge/search",
            request_body=permission_body,
            expected_status=200,
            expected_warning="permission_filtered",
        )
    finally:
        app.dependency_overrides.clear()

    # Embedding and vector providers have distinct warning codes.
    failing_embedding = FakeKnowledgeEmbeddingClient(dim=DIM, fail=True)
    _seed_search_dependencies(embedding=failing_embedding)
    embedding_body = _base_payload(request_id="req_fixture_embedding_error", query="任意查询")
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/knowledge/search", json=embedding_body)
        _record(
            output_dir,
            "knowledge-embedding-error",
            response,
            method="POST",
            path="/api/v1/knowledge/search",
            request_body=embedding_body,
            expected_status=200,
            expected_warning="embedding_error",
        )
    finally:
        app.dependency_overrides.clear()

    failing_vector = FakeKnowledgeVectorIndexClient(dim=DIM, fail_search=True)
    _seed_search_dependencies(vector=failing_vector)
    vector_body = _base_payload(request_id="req_fixture_vector_error", query="任意查询")
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/knowledge/search", json=vector_body)
        _record(
            output_dir,
            "knowledge-vector-search-error",
            response,
            method="POST",
            path="/api/v1/knowledge/search",
            request_body=vector_body,
            expected_status=200,
            expected_warning="vector_search_error",
        )
    finally:
        app.dependency_overrides.clear()

    # A slow provider degrades to timeout rather than raising transport failure.
    slow_vector = _SlowFakeVectorIndexClient(dim=DIM)
    repository, embedding, _ = _seed_search_dependencies(vector=slow_vector)
    timeout_settings = get_settings().model_copy(
        update={"knowledge_embedding_dim": DIM, "knowledge_search_timeout_seconds": 0.01}
    )
    app.dependency_overrides[get_settings] = lambda: timeout_settings
    timeout_asset = KnowledgeAsset(
        asset_id="asset_fixture_timeout",
        source_type="excel",
        source_name="慢速契约表",
        business_domain="客户运营",
    )
    timeout_chunk = KnowledgeChunk(
        chunk_id="chunk_fixture_timeout",
        asset_id=timeout_asset.asset_id,
        content="慢速 Provider 契约测试内容。",
        source_ref=KnowledgeSourceRef(source_type="excel", sheet="慢速", row=1),
        business_domain="客户运营",
        content_hash="hash_fixture_timeout",
    )
    _index_chunk(repository, embedding, slow_vector, asset=timeout_asset, chunk=timeout_chunk)
    timeout_body = _base_payload(request_id="req_fixture_timeout", query="慢速查询")
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/knowledge/search", json=timeout_body)
        _record(
            output_dir,
            "knowledge-timeout",
            response,
            method="POST",
            path="/api/v1/knowledge/search",
            request_body=timeout_body,
            expected_status=200,
            expected_warning="timeout",
        )
    finally:
        app.dependency_overrides.clear()

    # Exact Read enforces configured budgets and returns structured truncation warnings.
    repository, embedding, vector = _seed_dependencies(
        settings_update={"knowledge_read_limit_max": 1, "knowledge_read_max_chars_max": 5}
    )
    capped_asset = _asset("asset_fixture_caps")
    capped_chunk = _chunk("chunk_fixture_caps", capped_asset.asset_id, "abcdefghi")
    _index_chunk(repository, embedding, vector, asset=capped_asset, chunk=capped_chunk)
    truncation_body = {
        "request_id": "req_fixture_truncated",
        "session_id": "sess_fixture",
        "user_id": "user_fixture",
        "consumer": "central",
        "consumer_id": "fixture_central",
        "purpose": "answer",
        "target": {"type": "asset", "asset_id": capped_asset.asset_id},
        "return_options": {"limit": 99, "max_chars": 99},
    }
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/knowledge/read", json=truncation_body)
        _record(
            output_dir,
            "knowledge-result-truncated",
            response,
            method="POST",
            path="/api/v1/knowledge/read",
            request_body=truncation_body,
            expected_status=200,
            expected_warning="result_truncated",
        )
    finally:
        app.dependency_overrides.clear()

    fixtures = sorted(output_dir.glob("*.json"))
    if len(fixtures) != 9:
        raise RuntimeError(f"期望 9 个错误/降级 fixture，实际 {len(fixtures)}")
    print(f"已抓取 {len(fixtures)} 个 IRS 错误/降级 fixture 到 {output_dir}")


if __name__ == "__main__":
    main()
