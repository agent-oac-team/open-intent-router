#!/usr/bin/env python3
"""使用 IRS 的确定性测试替身抓取 22 个迁移接口成功 fixture。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ADMIN_TOKEN = "fixture-admin-token"
KNOWLEDGE_ADMIN_TOKEN = "test-admin-token"
FIXTURE_HEADERS = {
    "X-Admin-Sync-Token": ADMIN_TOKEN,
    "X-OAC-Username": "fixture-admin",
    "X-OAC-User-Display-Name": "Fixture Admin",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--irs-root",
        type=Path,
        default=Path("../intent_recon_sys"),
        help="IRS 仓库根目录",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/contract/oac_irs/irs-baseline/v1/success"),
    )
    return parser.parse_args()


def _scrub(value: Any, *, temporary_root: str) -> Any:
    if isinstance(value, dict):
        return {key: _scrub(child, temporary_root=temporary_root) for key, child in value.items()}
    if isinstance(value, list):
        return [_scrub(child, temporary_root=temporary_root) for child in value]
    if isinstance(value, str):
        return (
            value.replace(ADMIN_TOKEN, "<redacted-admin-token>")
            .replace(KNOWLEDGE_ADMIN_TOKEN, "<redacted-admin-token>")
            .replace(temporary_root, "<fixture-storage>")
        )
    return value


def _response_body(response: Any) -> Any:
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        return response.json()
    return response.text


def _capture(
    output_dir: Path,
    name: str,
    response: Any,
    *,
    method: str,
    path: str,
    request_headers: dict[str, str] | None = None,
    request_body: Any = None,
    query: dict[str, Any] | None = None,
    temporary_root: str,
) -> None:
    if not 200 <= response.status_code < 300:
        raise RuntimeError(
            f"{method} {path} 期望成功，实际 HTTP {response.status_code}: {response.text}"
        )
    payload = {
        "name": name,
        "request": {
            "method": method,
            "path": path,
            "headers": request_headers or {},
            "query": query or {},
            "body": request_body,
        },
        "response": {
            "status": response.status_code,
            "headers": {
                "content-type": response.headers.get("content-type", ""),
            },
            "body": _response_body(response),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{name}.json").write_text(
        json.dumps(_scrub(payload, temporary_root=temporary_root), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _configure_imports(irs_root: Path) -> None:
    resolved = irs_root.resolve()
    if not (resolved / "app" / "main.py").exists():
        raise RuntimeError(f"无效 IRS 根目录: {resolved}")
    sys.path.insert(0, str(resolved))


def main() -> None:  # noqa: C901, PLR0915
    args = _parse_args()
    _configure_imports(args.irs_root)

    from app.config import get_settings
    from fastapi.testclient import TestClient
    from tests.test_admin_api import StubFeishuClient, StubRepository, StubSyncService
    from tests.test_knowledge_admin_api import _seed as seed_admin_knowledge
    from tests.test_knowledge_admin_api import _workbook_bytes
    from tests.test_knowledge_generic_access_api import (
        _asset,
        _chunk,
        _index_chunk,
        _seed_dependencies,
    )

    from app.dependencies import (
        get_agent_registry_sync_repository,
        get_agent_registry_sync_service,
        get_feishu_bitable_client,
        get_llm_router_service,
    )
    from app.main import app
    from app.schemas.knowledge import KnowledgeSourceRef
    from tests.test_api import override_router_service

    output_dir = args.output_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="oir-irs-fixtures-") as temporary_root:
        # Central: deterministic LLM plus in-memory runtime repositories.
        app.dependency_overrides[get_llm_router_service] = override_router_service
        with TestClient(app) as client:
            route_body = {
                "request_id": "req_fixture_route",
                "session_id": "sess_fixture",
                "user_id": "user_fixture",
                "user_tags": ["运营版"],
                "source": "central_chat",
                "user_query": "帮我写一条企微文案",
                "central_chat_history": [],
                "agent_chat_history": [],
            }
            response = client.post("/api/v1/central/route", json=route_body)
            _capture(
                output_dir,
                "central-route",
                response,
                method="POST",
                path="/api/v1/central/route",
                request_body=route_body,
                temporary_root=temporary_root,
            )

            navigation_body = {
                "event_type": "navigation",
                "session_id": "sess_fixture",
                "user_id": "user_fixture",
                "from": "/home",
                "to": "/customer",
                "reason": "fixture_navigation",
            }
            response = client.post("/api/v1/central/events/navigation", json=navigation_body)
            _capture(
                output_dir,
                "central-navigation-event",
                response,
                method="POST",
                path="/api/v1/central/events/navigation",
                request_body=navigation_body,
                temporary_root=temporary_root,
            )

            agent_event_body = {
                "event_id": "evt_fixture_agent",
                "event_type": "agent_result",
                "session_id": "sess_fixture",
                "agent_id": "daily_wecom_task",
                "status": "completed",
                "artifact_refs": ["artifact_fixture_001"],
                "message": "测试智能体已完成。",
                "output": {"result": "fixture result"},
            }
            response = client.post("/api/v1/central/events/agent", json=agent_event_body)
            _capture(
                output_dir,
                "central-agent-event",
                response,
                method="POST",
                path="/api/v1/central/events/agent",
                request_body=agent_event_body,
                temporary_root=temporary_root,
            )

            plan_route_body = {
                "request_id": "req_plan",
                "session_id": "sess_fixture_plan",
                "user_id": "user_fixture",
                "user_tags": ["运营版"],
                "source": "central_chat",
                "user_query": "写文案并做成海报",
            }
            plan_route = client.post("/api/v1/central/route", json=plan_route_body)
            if plan_route.status_code != 200:
                raise RuntimeError(f"创建 fixture Plan 失败: {plan_route.text}")
            response = client.post("/api/v1/central/plans/plan_001/confirm")
            _capture(
                output_dir,
                "central-plan-confirm",
                response,
                method="POST",
                path="/api/v1/central/plans/{plan_id}/confirm",
                query={"plan_id": "plan_001"},
                temporary_root=temporary_root,
            )
        app.dependency_overrides.clear()

        # Registry: fake Feishu/repository preserve real validation and serialization.
        registry_settings = get_settings().model_copy(
            update={
                "admin_sync_token": ADMIN_TOKEN,
                "feishu_bitable_app_token": "fixture-base",
                "feishu_bitable_table_id": "fixture-table",
            }
        )
        StubRepository.items = {}
        StubFeishuClient.record_id_by_agent_id = {}
        app.dependency_overrides[get_settings] = lambda: registry_settings
        app.dependency_overrides[get_feishu_bitable_client] = StubFeishuClient
        app.dependency_overrides[get_agent_registry_sync_repository] = StubRepository
        app.dependency_overrides[get_agent_registry_sync_service] = StubSyncService
        registry_body = {
            "agent_id": "fixture_agent",
            "name": "契约测试智能体",
            "description": "用于迁移契约测试",
            "bot_id": "",
            "route_path": "/fixture",
            "allowed_user_tags": ["运营版"],
            "positive_keywords": ["契约"],
            "negative_keywords": ["忽略"],
            "enabled": True,
        }
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/admin/agent-registry", headers=FIXTURE_HEADERS, json=registry_body
            )
            _capture(
                output_dir,
                "registry-create",
                response,
                method="POST",
                path="/api/v1/admin/agent-registry",
                request_headers=FIXTURE_HEADERS,
                request_body=registry_body,
                temporary_root=temporary_root,
            )

            response = client.get("/api/v1/admin/agent-registry", headers=FIXTURE_HEADERS)
            _capture(
                output_dir,
                "registry-list",
                response,
                method="GET",
                path="/api/v1/admin/agent-registry",
                request_headers=FIXTURE_HEADERS,
                temporary_root=temporary_root,
            )

            update_body = {**registry_body, "description": "更新后的契约测试智能体"}
            response = client.put(
                "/api/v1/admin/agent-registry/fixture_agent",
                headers=FIXTURE_HEADERS,
                json=update_body,
            )
            _capture(
                output_dir,
                "registry-update",
                response,
                method="PUT",
                path="/api/v1/admin/agent-registry/{agent_id}",
                request_headers=FIXTURE_HEADERS,
                request_body=update_body,
                query={"agent_id": "fixture_agent"},
                temporary_root=temporary_root,
            )

            enabled_body = {"enabled": False}
            response = client.patch(
                "/api/v1/admin/agent-registry/fixture_agent/enabled",
                headers=FIXTURE_HEADERS,
                json=enabled_body,
            )
            _capture(
                output_dir,
                "registry-enabled",
                response,
                method="PATCH",
                path="/api/v1/admin/agent-registry/{agent_id}/enabled",
                request_headers=FIXTURE_HEADERS,
                request_body=enabled_body,
                query={"agent_id": "fixture_agent"},
                temporary_root=temporary_root,
            )

            response = client.delete(
                "/api/v1/admin/agent-registry/fixture_agent", headers=FIXTURE_HEADERS
            )
            _capture(
                output_dir,
                "registry-delete",
                response,
                method="DELETE",
                path="/api/v1/admin/agent-registry/{agent_id}",
                request_headers=FIXTURE_HEADERS,
                query={"agent_id": "fixture_agent"},
                temporary_root=temporary_root,
            )
        app.dependency_overrides.clear()

        # Knowledge query/read: canonical in-memory asset plus fake embedding/vector index.
        repository, embedding, vector = _seed_dependencies()
        asset = _asset("asset_content_production_04", business_domain="内容生产")
        chunk = _chunk(
            "chunk_fixture_04_1",
            asset.asset_id,
            "活动名称：契约测试活动；活动期限：2026 年 12 月 31 日。",
            business_domain="内容生产",
            source_ref=KnowledgeSourceRef(source_type="excel", sheet="活动表", row=2),
        )
        _index_chunk(repository, embedding, vector, asset=asset, chunk=chunk)
        search_body = {
            "request_id": "req_fixture_search",
            "session_id": "sess_fixture_knowledge",
            "user_id": "user_fixture",
            "user_tags": ["运营版"],
            "consumer": "coze_workflow",
            "consumer_id": "fixture_workflow",
            "purpose": "workflow",
            "query": "契约测试活动期限",
            "scope": {"asset_ids": [asset.asset_id]},
            "return_options": {"include_structured_payload": True},
            "top_k": 3,
        }
        with TestClient(app) as client:
            response = client.post("/api/v1/knowledge/search", json=search_body)
            _capture(
                output_dir,
                "knowledge-search",
                response,
                method="POST",
                path="/api/v1/knowledge/search",
                request_body=search_body,
                temporary_root=temporary_root,
            )

            grouped_body = {
                "request_id": "req_fixture_grouped",
                "session_id": "sess_fixture_knowledge",
                "user_id": "user_fixture",
                "user_tags": ["运营版"],
                "consumer": "coze_workflow",
                "consumer_id": "fixture_workflow",
                "purpose": "workflow",
                "query": "契约测试活动期限",
                "asset_group": "content_production",
                "asset_keys": ["04"],
                "top_k_per_asset": 3,
                "include_empty_assets": True,
            }
            response = client.post("/api/v1/knowledge/grouped-search", json=grouped_body)
            _capture(
                output_dir,
                "knowledge-grouped-search",
                response,
                method="POST",
                path="/api/v1/knowledge/grouped-search",
                request_body=grouped_body,
                temporary_root=temporary_root,
            )

            read_body = {
                "request_id": "req_fixture_read",
                "session_id": "sess_fixture_knowledge",
                "user_id": "user_fixture",
                "user_tags": ["运营版"],
                "consumer": "central",
                "consumer_id": "fixture_central",
                "purpose": "answer",
                "target": {"type": "asset", "asset_id": asset.asset_id},
                "return_options": {"include_structured_payload": True},
            }
            response = client.post("/api/v1/knowledge/read", json=read_body)
            _capture(
                output_dir,
                "knowledge-read",
                response,
                method="POST",
                path="/api/v1/knowledge/read",
                request_body=read_body,
                temporary_root=temporary_root,
            )

            query = {"user_id": "user_fixture", "user_tags": "运营版"}
            response = client.get("/api/v1/knowledge/assets", params=query)
            _capture(
                output_dir,
                "knowledge-assets-list",
                response,
                method="GET",
                path="/api/v1/knowledge/assets",
                query=query,
                temporary_root=temporary_root,
            )
            response = client.get(f"/api/v1/knowledge/assets/{asset.asset_id}", params=query)
            _capture(
                output_dir,
                "knowledge-asset-detail",
                response,
                method="GET",
                path="/api/v1/knowledge/assets/{asset_id}",
                query={**query, "asset_id": asset.asset_id},
                temporary_root=temporary_root,
            )
            response = client.get(f"/api/v1/knowledge/assets/{asset.asset_id}/chunks", params=query)
            _capture(
                output_dir,
                "knowledge-asset-chunks",
                response,
                method="GET",
                path="/api/v1/knowledge/assets/{asset_id}/chunks",
                query={**query, "asset_id": asset.asset_id},
                temporary_root=temporary_root,
            )
            response = client.get(f"/api/v1/knowledge/chunks/{chunk.chunk_id}", params=query)
            _capture(
                output_dir,
                "knowledge-chunk-detail",
                response,
                method="GET",
                path="/api/v1/knowledge/chunks/{chunk_id}",
                query={**query, "chunk_id": chunk.chunk_id},
                temporary_root=temporary_root,
            )
        app.dependency_overrides.clear()

        # Knowledge Admin: upload once, then exercise every management method.
        seed_admin_knowledge(Path(temporary_root))
        knowledge_admin_headers = {
            **FIXTURE_HEADERS,
            "X-Admin-Sync-Token": KNOWLEDGE_ADMIN_TOKEN,
        }
        upload_request = {
            "source_name": "契约测试活动表",
            "business_domain": "内容生产",
            "sensitivity": "internal",
            "allowed_user_tags": "运营版",
            "file": {
                "filename": "fixture.xlsx",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        }
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/admin/knowledge/files",
                headers=knowledge_admin_headers,
                data={key: value for key, value in upload_request.items() if key != "file"},
                files={
                    "file": (
                        "fixture.xlsx",
                        _workbook_bytes(),
                        upload_request["file"]["content_type"],
                    )
                },
            )
            _capture(
                output_dir,
                "knowledge-admin-upload",
                response,
                method="POST",
                path="/api/v1/admin/knowledge/files",
                request_headers=knowledge_admin_headers,
                request_body=upload_request,
                temporary_root=temporary_root,
            )
            asset_id = response.json()["asset"]["asset_id"]

            response = client.get("/api/v1/admin/knowledge/files", headers=knowledge_admin_headers)
            _capture(
                output_dir,
                "knowledge-admin-list",
                response,
                method="GET",
                path="/api/v1/admin/knowledge/files",
                request_headers=knowledge_admin_headers,
                temporary_root=temporary_root,
            )
            response = client.get(
                f"/api/v1/admin/knowledge/files/{asset_id}", headers=knowledge_admin_headers
            )
            _capture(
                output_dir,
                "knowledge-admin-detail",
                response,
                method="GET",
                path="/api/v1/admin/knowledge/files/{asset_id}",
                request_headers=knowledge_admin_headers,
                query={"asset_id": asset_id},
                temporary_root=temporary_root,
            )
            response = client.get(
                f"/api/v1/admin/knowledge/files/{asset_id}/chunks",
                headers=knowledge_admin_headers,
            )
            _capture(
                output_dir,
                "knowledge-admin-chunks",
                response,
                method="GET",
                path="/api/v1/admin/knowledge/files/{asset_id}/chunks",
                request_headers=knowledge_admin_headers,
                query={"asset_id": asset_id},
                temporary_root=temporary_root,
            )
            response = client.post(
                f"/api/v1/admin/knowledge/files/{asset_id}/retry",
                headers=knowledge_admin_headers,
            )
            _capture(
                output_dir,
                "knowledge-admin-retry",
                response,
                method="POST",
                path="/api/v1/admin/knowledge/files/{asset_id}/retry",
                request_headers=knowledge_admin_headers,
                query={"asset_id": asset_id},
                temporary_root=temporary_root,
            )
            delete_body = {"reason": "fixture cleanup"}
            response = client.request(
                "DELETE",
                f"/api/v1/admin/knowledge/files/{asset_id}",
                headers=knowledge_admin_headers,
                json=delete_body,
            )
            _capture(
                output_dir,
                "knowledge-admin-delete",
                response,
                method="DELETE",
                path="/api/v1/admin/knowledge/files/{asset_id}",
                request_headers=knowledge_admin_headers,
                request_body=delete_body,
                query={"asset_id": asset_id},
                temporary_root=temporary_root,
            )
        app.dependency_overrides.clear()

    fixtures = sorted(output_dir.glob("*.json"))
    if len(fixtures) != 22:
        raise RuntimeError(f"期望 22 个成功 fixture，实际 {len(fixtures)}")
    print(f"已抓取 {len(fixtures)} 个 IRS 成功 fixture 到 {output_dir}")


if __name__ == "__main__":
    main()
