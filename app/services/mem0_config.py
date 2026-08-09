import importlib.util
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
                "collection_name": _effective_milvus_collection(settings),
                "embedding_model_dims": embedding_dims,
                "url": _effective_milvus_uri(settings),
                "token": _effective_milvus_token(settings) or "",
            },
        },
        "embedder": {
            "provider": settings.memory_mem0_embedder_provider,
            "config": {
                "model": embedding_model,
                "embedding_dims": embedding_dims,
            },
        },
        "history_db_path": settings.memory_mem0_history_db_path or settings.mem0_history_db_path,
    }
    vector_config = config["vector_store"]["config"]
    if milvus_db_name := _effective_milvus_db_name(settings):
        vector_config["db_name"] = milvus_db_name
    embedder_config = config["embedder"]["config"]
    if embedding_api_key:
        embedder_config["api_key"] = embedding_api_key
    if embedding_base_url:
        embedder_config["openai_base_url"] = embedding_base_url
    llm_config = _llm_config(settings)
    if llm_config:
        config["llm"] = llm_config
    return config


def mem0_static_metadata(settings: Settings) -> dict[str, Any]:
    infrastructure = memory_infrastructure_metadata(settings)
    return {
        "provider": settings.memory_strategy_provider,
        "fail_closed": settings.memory_mem0_fail_closed_effective,
        "vector_provider": settings.memory_mem0_vector_provider,
        "collection": infrastructure["milvus_collection"],
        "milvus_uri": infrastructure["milvus_uri"],
        "history_backend": settings.memory_mem0_history_backend,
        "history_canonical": _history_canonical(settings),
        "embedding_model": _effective_embedding_model(settings),
        "embedding_dims": _effective_embedding_dims(settings),
        "embedding_base_url_configured": bool(_effective_embedding_base_url(settings)),
        "embedding_api_key_configured": bool(_effective_embedding_api_key(settings)),
        "llm_provider": settings.memory_mem0_llm_provider,
        "llm_model": _effective_llm_model(settings),
        "llm_api_key_configured": bool(_effective_llm_api_key(settings)),
        "configuration_sources": infrastructure["configuration_sources"],
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


def _effective_embedding_model(settings: Settings) -> str | None:
    return settings.memory_embedding_model


def _effective_embedding_dims(settings: Settings) -> int | None:
    return settings.memory_embedding_dims


def _effective_embedding_base_url(settings: Settings) -> str | None:
    return settings.memory_embedding_base_url


def _effective_embedding_api_key(settings: Settings) -> str | None:
    return settings.memory_embedding_api_key


def memory_infrastructure_metadata(settings: Settings) -> dict[str, Any]:
    sources = {
        "database_url": _source(
            settings,
            "memory_database_url",
            "MEMORY_DATABASE_URL",
        ),
        "milvus_uri": _source(
            settings,
            "memory_milvus_uri",
            "MEMORY_MILVUS_URI",
        ),
        "milvus_token": _source(
            settings,
            "memory_milvus_token",
            "MEMORY_MILVUS_TOKEN",
        ),
        "milvus_db_name": _source(
            settings,
            "memory_milvus_db_name",
            "MEMORY_MILVUS_DB_NAME",
        ),
        "milvus_collection": _source(
            settings,
            "memory_milvus_collection",
            "MEMORY_MILVUS_COLLECTION",
        ),
        "embedding_base_url": _source(
            settings,
            "memory_embedding_base_url",
            "MEMORY_EMBEDDING_BASE_URL",
        ),
        "embedding_api_key": _source(
            settings,
            "memory_embedding_api_key",
            "MEMORY_EMBEDDING_API_KEY",
        ),
        "embedding_model": _source(
            settings,
            "memory_embedding_model",
            "MEMORY_EMBEDDING_MODEL",
        ),
        "embedding_dims": _source(
            settings,
            "memory_embedding_dims",
            "MEMORY_EMBEDDING_DIMS",
        ),
    }
    return {
        "database_url": settings.effective_memory_database_url,
        "milvus_uri": _effective_milvus_uri(settings),
        "milvus_collection": _effective_milvus_collection(settings),
        "embedding_model": _effective_embedding_model(settings),
        "embedding_dims": _effective_embedding_dims(settings),
        "configuration_sources": sources,
    }


def _effective_milvus_uri(settings: Settings) -> str | None:
    return settings.effective_memory_milvus_uri


def _effective_milvus_token(settings: Settings) -> str | None:
    return settings.effective_memory_milvus_token


def _effective_milvus_db_name(settings: Settings) -> str | None:
    return settings.effective_memory_milvus_db_name


def _effective_milvus_collection(settings: Settings) -> str | None:
    return settings.effective_memory_milvus_collection


def _source(
    settings: Settings,
    field: str,
    name: str,
) -> str:
    return name if getattr(settings, field) is not None else "unconfigured"


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
    return settings.memory_mem0_llm_model


def _effective_llm_api_key(settings: Settings) -> str | None:
    return settings.memory_mem0_llm_api_key


def _effective_llm_base_url(settings: Settings) -> str | None:
    return settings.memory_mem0_llm_base_url


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
