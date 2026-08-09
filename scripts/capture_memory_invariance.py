#!/usr/bin/env python3
"""Capture a comparable, body-free OIR Memory configuration and behavior report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from app.core.security import memory_identity_signature

SCHEMA_VERSION = "oir-memory-invariance-v1"
_DEBUG_LIMIT = 100


def capture_memory_invariance(
    client,
    *,
    tenant_id: str,
    user_id: str,
    recall_query: str,
    identity_secret: str | None = None,
    exercise_crud: bool = False,
    probe_content: str | None = None,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Use public OIR APIs and retain only configuration, identifiers, counts and statuses."""
    headers = _identity_headers(
        tenant_id=tenant_id,
        user_id=user_id,
        identity_secret=identity_secret,
    )
    runtime = _json_response(client.get("/api/v1/runtime/config"))
    before = _debug_snapshot(client, headers=headers)
    representative = _recall_snapshot(
        client,
        tenant_id=tenant_id,
        user_id=user_id,
        query=recall_query,
    )
    crud = {"executed": False}
    if exercise_crud:
        crud = _exercise_crud(
            client,
            tenant_id=tenant_id,
            user_id=user_id,
            headers=headers,
            content=probe_content or f"memory-invariance-probe-{uuid4().hex}",
        )
    after = _debug_snapshot(client, headers=headers)
    configuration = {
        "memory_mode": runtime.get("memory_mode"),
        "memory_strategy_provider": runtime.get("memory_strategy_provider"),
        "memory_database_url": runtime.get("memory_database_url"),
        "memory_database_source": runtime.get("memory_infrastructure_sources", {}).get(
            "database_url"
        ),
        "memory_milvus_uri": runtime.get("memory_mem0_milvus_uri"),
        "memory_milvus_collection": runtime.get("memory_mem0_collection"),
        "memory_embedding_model": runtime.get("memory_embedding_model"),
        "memory_embedding_dims": runtime.get("memory_embedding_dims"),
        "configuration_sources": runtime.get("memory_infrastructure_sources", {}),
    }
    comparison = {
        "configuration": {
            key: configuration[key]
            for key in (
                "memory_mode",
                "memory_strategy_provider",
                "memory_milvus_collection",
                "memory_embedding_model",
                "memory_embedding_dims",
            )
        },
        "active_item_count_before": before["active_item_count"],
        "active_item_count_after": after["active_item_count"],
        "representative_recall": representative,
        "crud_behavior": {
            key: crud.get(key)
            for key in (
                "executed",
                "write_status",
                "recall_found",
                "delete_status",
                "post_delete_found",
            )
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": (captured_at or datetime.now(UTC)).isoformat(),
        "subject": {
            "tenant_id_sha256": _sha256(tenant_id),
            "user_id_sha256": _sha256(user_id),
        },
        "effective_configuration": configuration,
        "storage": {
            "collection": configuration["memory_milvus_collection"],
            "active_item_count_before": before["active_item_count"],
            "active_item_count_after": after["active_item_count"],
            "count_limit": _DEBUG_LIMIT,
            "count_truncated": before["count_truncated"] or after["count_truncated"],
        },
        "representative_recall": representative,
        "crud_probe": crud,
        "content_policy": {"memory_body_included": False},
        "comparison_fingerprint": _sha256(_stable_json(comparison)),
    }


def _debug_snapshot(client, *, headers: dict[str, str]) -> dict[str, Any]:
    payload = _json_response(
        client.get(
            f"/api/v1/memories/debug?limit={_DEBUG_LIMIT}",
            headers=headers,
        )
    )
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    active = [
        item
        for item in items
        if isinstance(item, dict) and item.get("lifecycle_status") == "active"
    ]
    return {
        "active_item_count": len(active),
        "count_truncated": len(items) >= _DEBUG_LIMIT,
    }


def _recall_snapshot(
    client,
    *,
    tenant_id: str,
    user_id: str,
    query: str,
) -> dict[str, Any]:
    payload = _json_response(
        client.post(
            "/api/v1/memories/recall",
            json={
                "query": query,
                "user": {"id": user_id, "attributes": {"tenant_id": tenant_id}},
                "scopes": ["user_preference", "stable_fact"],
                "max_items": 5,
            },
        )
    )
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    items = context.get("items") if isinstance(context.get("items"), list) else []
    return {
        "query_sha256": _sha256(query),
        "status": context.get("status"),
        "item_count": len(items),
        "memory_ids": [
            str(item["memory_id"])
            for item in items
            if isinstance(item, dict) and item.get("memory_id")
        ],
    }


def _exercise_crud(
    client,
    *,
    tenant_id: str,
    user_id: str,
    headers: dict[str, str],
    content: str,
) -> dict[str, Any]:
    probe_id = uuid4().hex
    decisions = _json_response(
        client.post(
            "/api/v1/memories/write-candidates",
            params={"user_id": user_id, "tenant_id": tenant_id},
            json=[
                {
                    "scope": "stable_fact",
                    "content": content,
                    "source": "memory_invariance_probe",
                    "confidence": 1.0,
                    "importance": 0.1,
                }
            ],
        )
    )
    decision = decisions[0] if isinstance(decisions, list) and decisions else {}
    memory_id = decision.get("memory_id") if isinstance(decision, dict) else None
    recalled = _recall_snapshot(
        client,
        tenant_id=tenant_id,
        user_id=user_id,
        query=content,
    )
    delete_status = None
    if memory_id:
        deletion = _json_response(
            client.request(
                "DELETE",
                f"/api/v1/memories/{memory_id}",
                headers=headers,
                json={
                    "idempotency_key": f"memory-invariance-delete-{probe_id}",
                    "reason": "reversible Memory invariance probe cleanup",
                    "expected_revision_id": decision.get("current_revision_id"),
                },
            )
        )
        delete_status = deletion.get("status")
    post_delete = _recall_snapshot(
        client,
        tenant_id=tenant_id,
        user_id=user_id,
        query=content,
    )
    return {
        "executed": True,
        "probe_content_sha256": _sha256(content),
        "write_status": decision.get("status") if isinstance(decision, dict) else None,
        "memory_id": memory_id,
        "recall_found": bool(memory_id and memory_id in recalled["memory_ids"]),
        "delete_status": delete_status,
        "post_delete_found": bool(memory_id and memory_id in post_delete["memory_ids"]),
    }


def _identity_headers(
    *,
    tenant_id: str,
    user_id: str,
    identity_secret: str | None,
) -> dict[str, str]:
    headers = {"X-Tenant-ID": tenant_id, "X-User-ID": user_id}
    if identity_secret:
        headers["X-Memory-Identity-Signature"] = memory_identity_signature(
            tenant_id=tenant_id,
            user_id=user_id,
            secret=identity_secret,
        )
    return headers


def _json_response(response) -> Any:
    response.raise_for_status()
    return response.json()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--recall-query", required=True)
    parser.add_argument(
        "--identity-secret-env",
        default="MEMORY_IDENTITY_SECRET",
        help="Environment variable containing the Memory identity secret; the value is never output.",
    )
    parser.add_argument(
        "--exercise-crud",
        action="store_true",
        help="Create, recall and delete one uniquely identified probe Memory.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    secret = os.environ.get(args.identity_secret_env)
    with httpx.Client(base_url=args.base_url, timeout=15.0) as client:
        report = capture_memory_invariance(
            client,
            tenant_id=args.tenant_id,
            user_id=args.user_id,
            recall_query=args.recall_query,
            identity_secret=secret,
            exercise_crud=args.exercise_crud,
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
