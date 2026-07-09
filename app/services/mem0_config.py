import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.config import Settings


def build_mem0_config(settings: Settings) -> dict[str, Any]:
    embedding_model = _effective_embedding_model(settings)
    embedding_dims = _effective_embedding_dims(settings)
    embedding_base_url = _effective_embedding_base_url(settings)
    embedding_api_key = _effective_embedding_api_key(settings)
    config: dict[str, Any] = {
        "vector_store": {
            "provider": settings.memory_mem0_vector_provider,
            "config": {
                "collection_name": settings.memory_mem0_milvus_collection,
                "embedding_model_dims": embedding_dims,
                "url": settings.memory_mem0_milvus_uri,
                "token": settings.memory_mem0_milvus_token or "",
            },
        },
        "embedder": {
            "provider": settings.memory_mem0_embedder_provider,
            "config": {
                "model": settings.memory_mem0_embedder_model or embedding_model,
                "embedding_dims": embedding_dims,
            },
        },
        "history_db_path": settings.memory_mem0_history_db_path or settings.mem0_history_db_path,
    }
    vector_config = config["vector_store"]["config"]
    if settings.memory_mem0_milvus_db_name:
        vector_config["db_name"] = settings.memory_mem0_milvus_db_name
    embedder_config = config["embedder"]["config"]
    if embedding_api_key:
        embedder_config["api_key"] = embedding_api_key
    if embedding_base_url:
        embedder_config["openai_base_url"] = embedding_base_url
    llm_config = _llm_config(settings)
    if llm_config:
        config["llm"] = llm_config
    if settings.mem0_config_json.strip():
        override = json.loads(settings.mem0_config_json)
        if not isinstance(override, dict):
            raise ValueError("MEM0_CONFIG_JSON must decode to a JSON object")
        config = _deep_merge(config, override)
    return config


def mem0_static_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "provider": settings.memory_strategy_provider,
        "fail_closed": settings.memory_mem0_fail_closed_effective,
        "vector_provider": settings.memory_mem0_vector_provider,
        "collection": settings.memory_mem0_milvus_collection,
        "milvus_uri": settings.memory_mem0_milvus_uri,
        "history_backend": settings.memory_mem0_history_backend,
        "history_canonical": _history_canonical(settings),
        "embedding_model": _effective_embedding_model(settings),
        "embedding_dims": _effective_embedding_dims(settings),
        "embedding_base_url_configured": bool(_effective_embedding_base_url(settings)),
        "embedding_api_key_configured": bool(_effective_embedding_api_key(settings)),
        "llm_provider": settings.memory_mem0_llm_provider,
        "llm_model": _effective_llm_model(settings),
        "llm_api_key_configured": bool(_effective_llm_api_key(settings)),
        "knowledge_transition_collections": {
            "memory": settings.memory_mem0_milvus_collection,
            "irs_knowledge": "oac_knowledge_chunks",
            "oir_knowledge": settings.knowledge_milvus_collection,
        },
    }


def mem0_health_check(settings: Settings) -> dict[str, Any]:
    metadata = mem0_static_metadata(settings)
    checks: list[dict[str, Any]] = []
    if settings.memory_strategy_provider != "mem0":
        return {**metadata, "status": "disabled", "checks": checks}
    try:
        config = build_mem0_config(settings)
        checks.append({"name": "config", "status": "ok"})
    except Exception as exc:
        checks.append({"name": "config", "status": "error", "reason": str(exc)})
        return {**metadata, "status": "error", "checks": checks}
    checks.append(
        {
            "name": "mem0_dependency",
            "status": "ok" if importlib.util.find_spec("mem0") else "missing",
        }
    )
    vector_config = config.get("vector_store", {}).get("config", {})
    uri = str(vector_config.get("url") or "")
    if config.get("vector_store", {}).get("provider") == "milvus":
        path_status = _milvus_lite_path_status(uri)
        checks.append({"name": "milvus_lite_uri", **path_status})
    embedding_config = config.get("embedder", {}).get("config", {})
    if embedding_config.get("api_key"):
        checks.append({"name": "embedding_api_key", "status": "ok"})
    else:
        checks.append({"name": "embedding_api_key", "status": "missing"})
    vector_dims = vector_config.get("embedding_model_dims")
    embedder_dims = embedding_config.get("embedding_dims")
    if vector_dims and embedder_dims and int(vector_dims) != int(embedder_dims):
        checks.append(
            {
                "name": "embedding_dimension",
                "status": "mismatch",
                "vector_dims": vector_dims,
                "embedder_dims": embedder_dims,
            }
        )
    else:
        checks.append({"name": "embedding_dimension", "status": "ok"})
    status = "ok"
    if any(check["status"] in {"error", "mismatch"} for check in checks):
        status = "error"
    elif any(check["status"] in {"missing", "unavailable"} for check in checks):
        status = "degraded"
    return {**metadata, "status": status, "checks": checks}


def _effective_embedding_model(settings: Settings) -> str:
    return (
        settings.memory_mem0_embedding_model
        or settings.embedding_model
        or settings.knowledge_embedding_model
    )


def _effective_embedding_dims(settings: Settings) -> int:
    return (
        settings.memory_mem0_embedding_dims
        or settings.embedding_dim
        or settings.knowledge_embedding_dim
    )


def _effective_embedding_base_url(settings: Settings) -> str | None:
    return (
        settings.memory_mem0_embedding_base_url
        or settings.embedding_base_url
        or settings.knowledge_embedding_base_url
    )


def _effective_embedding_api_key(settings: Settings) -> str | None:
    return (
        settings.memory_mem0_embedding_api_key
        or settings.embedding_api_key
        or settings.knowledge_embedding_api_key
    )


def _llm_config(settings: Settings) -> dict[str, Any] | None:
    fields = {
        "model": _effective_llm_model(settings),
        "api_key": _effective_llm_api_key(settings),
        "openai_base_url": _effective_llm_base_url(settings),
    }
    values = {key: value for key, value in fields.items() if value}
    if not values:
        return None
    return {"provider": settings.memory_mem0_llm_provider, "config": values}


def _effective_llm_model(settings: Settings) -> str | None:
    return settings.memory_mem0_llm_model or (
        settings.router_llm_model if settings.router_llm_provider == "openai_compatible" else None
    )


def _effective_llm_api_key(settings: Settings) -> str | None:
    return settings.memory_mem0_llm_api_key or (
        settings.router_llm_api_key if settings.router_llm_provider == "openai_compatible" else None
    )


def _effective_llm_base_url(settings: Settings) -> str | None:
    return settings.memory_mem0_llm_base_url or (
        settings.router_llm_base_url
        if settings.router_llm_provider == "openai_compatible"
        else None
    )


def _history_canonical(settings: Settings) -> str:
    if settings.memory_mem0_history_backend == "postgresql":
        return "oir_memory_events_ledger"
    if settings.memory_mem0_history_backend == "sqlite":
        return "mem0_sdk_sqlite"
    return "none"


def _milvus_lite_path_status(uri: str) -> dict[str, str]:
    if not uri:
        return {"status": "unavailable", "reason": "empty_uri"}
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https", "tcp", "grpc"}:
        return {"status": "ok"}
    path = Path(uri)
    parent = path.parent if path.suffix else path
    if parent.exists() and parent.is_dir():
        return {"status": "ok"}
    return {"status": "unavailable", "reason": "parent_directory_missing"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
