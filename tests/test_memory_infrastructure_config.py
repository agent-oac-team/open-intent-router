import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.dependencies import get_memory_data_settings
from app.services.mem0_config import (
    build_mem0_config,
    mem0_static_metadata,
    memory_infrastructure_metadata,
)


def test_explicit_memory_infrastructure_settings_override_compatibility_sources() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///legacy-core.db",
        memory_database_url="sqlite+aiosqlite:///explicit-memory.db",
        memory_milvus_uri="https://memory-milvus.example",
        memory_milvus_token="explicit-token",
        memory_milvus_db_name="memory-db",
        memory_milvus_collection="stable-memory-vectors",
        memory_embedding_base_url="https://memory-embedding.example/v1",
        memory_embedding_api_key="explicit-embedding-key",
        memory_embedding_model="memory-embedding-v2",
        memory_embedding_dims=1536,
        knowledge_embedding_base_url="https://knowledge-embedding.example/v1",
        knowledge_embedding_api_key="knowledge-key",
        knowledge_embedding_model="knowledge-embedding-v1",
        knowledge_embedding_dim=1024,
    )

    config = build_mem0_config(settings)
    metadata = memory_infrastructure_metadata(settings)

    assert settings.effective_memory_database_url == "sqlite+aiosqlite:///explicit-memory.db"
    assert config["vector_store"]["config"] == {
        "collection_name": "stable-memory-vectors",
        "embedding_model_dims": 1536,
        "url": "https://memory-milvus.example",
        "token": "explicit-token",
        "db_name": "memory-db",
    }
    assert config["embedder"]["config"] == {
        "model": "memory-embedding-v2",
        "embedding_dims": 1536,
        "api_key": "explicit-embedding-key",
        "openai_base_url": "https://memory-embedding.example/v1",
    }
    assert metadata["configuration_sources"] == {
        "database_url": "MEMORY_DATABASE_URL",
        "milvus_uri": "MEMORY_MILVUS_URI",
        "milvus_token": "MEMORY_MILVUS_TOKEN",
        "milvus_db_name": "MEMORY_MILVUS_DB_NAME",
        "milvus_collection": "MEMORY_MILVUS_COLLECTION",
        "embedding_base_url": "MEMORY_EMBEDDING_BASE_URL",
        "embedding_api_key": "MEMORY_EMBEDDING_API_KEY",
        "embedding_model": "MEMORY_EMBEDDING_MODEL",
        "embedding_dims": "MEMORY_EMBEDDING_DIMS",
    }
    assert "knowledge_transition_collections" not in mem0_static_metadata(settings)


def test_enabled_mem0_rejects_missing_explicit_memory_infrastructure() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            memory_mode="on",
            memory_strategy_provider="mem0",
            memory_database_url=None,
            memory_milvus_uri=None,
            memory_milvus_collection=None,
            memory_embedding_model=None,
            memory_embedding_dims=None,
            knowledge_milvus_uri="https://knowledge-milvus.example",
            knowledge_milvus_collection="knowledge-vectors",
            knowledge_embedding_base_url="https://knowledge-embedding.example/v1",
            knowledge_embedding_api_key="knowledge-key",
            knowledge_embedding_model="knowledge-embedding-v1",
            knowledge_embedding_dim=768,
        )

    message = str(exc_info.value)
    assert "Memory is enabled but required explicit settings are missing" in message
    assert "MEMORY_DATABASE_URL" in message
    assert "MEMORY_MILVUS_URI" in message
    assert "MEMORY_MILVUS_COLLECTION" in message
    assert "MEMORY_EMBEDDING_MODEL" in message
    assert "MEMORY_EMBEDDING_DIMS" in message
    assert "KNOWLEDGE_" not in message


def test_knowledge_and_router_configuration_cannot_change_memory_config() -> None:
    settings = Settings(
        _env_file=None,
        memory_mode="on",
        memory_strategy_provider="mem0",
        memory_database_url="sqlite+aiosqlite:///existing-memory.db",
        memory_milvus_uri=".data/oir_memory_milvus.db",
        memory_milvus_collection="oir_memory_vectors",
        memory_embedding_model="text-embedding-v4",
        memory_embedding_dims=1024,
        knowledge_enabled=False,
        knowledge_embedding_base_url="https://knowledge-embedding.example/v1",
        knowledge_embedding_api_key="knowledge-key",
        knowledge_embedding_model="knowledge-embedding-v1",
        knowledge_embedding_dim=768,
        router_llm_provider="openai_compatible",
        router_llm_model="router-model",
        router_llm_base_url="https://router.example/v1",
        router_llm_api_key="router-key",
    )

    config = build_mem0_config(settings)

    assert config["vector_store"]["config"]["collection_name"] == "oir_memory_vectors"
    assert config["embedder"]["config"] == {
        "model": "text-embedding-v4",
        "embedding_dims": 1024,
    }
    assert "llm" not in config


def test_memory_bootstrap_uses_the_explicit_memory_database(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///core.db")
    monkeypatch.setenv("MEMORY_DATABASE_URL", "sqlite+aiosqlite:///memory.db")
    get_settings.cache_clear()
    get_memory_data_settings.cache_clear()
    try:
        assert get_memory_data_settings().database_url == "sqlite+aiosqlite:///memory.db"
    finally:
        get_memory_data_settings.cache_clear()
        get_settings.cache_clear()


def test_disabled_memory_does_not_require_infrastructure() -> None:
    settings = Settings(
        _env_file=None,
        memory_mode="off",
        memory_strategy_provider="mem0",
        memory_database_url=None,
        memory_milvus_uri=None,
        memory_milvus_collection=None,
        memory_embedding_model=None,
        memory_embedding_dims=None,
    )

    assert settings.memory_enabled is False


def test_explicit_memory_config_preserves_phase_one_invariance_baseline() -> None:
    evidence_path = (
        Path(__file__).parents[1]
        / "docs/App-Research/evidence/migration/oir-memory-invariance-local.json"
    )
    baseline = json.loads(evidence_path.read_text(encoding="utf-8"))
    effective = baseline["effective_configuration"]
    settings = Settings(
        _env_file=None,
        memory_mode=effective["memory_mode"],
        memory_strategy_provider=effective["memory_strategy_provider"],
        memory_database_url=effective["memory_database_url"],
        memory_milvus_uri=effective["memory_milvus_uri"],
        memory_milvus_collection=effective["memory_milvus_collection"],
        memory_embedding_model=effective["memory_embedding_model"],
        memory_embedding_dims=effective["memory_embedding_dims"],
    )
    metadata = memory_infrastructure_metadata(settings)

    assert metadata["milvus_collection"] == baseline["storage"]["collection"]
    assert metadata["embedding_model"] == effective["memory_embedding_model"]
    assert metadata["embedding_dims"] == effective["memory_embedding_dims"]
    assert (
        baseline["storage"]["active_item_count_before"]
        == baseline["storage"]["active_item_count_after"]
    )
    assert baseline["crud_probe"]["write_status"] == "accepted"
    assert baseline["crud_probe"]["recall_found"] is True
    assert baseline["crud_probe"]["post_delete_found"] is False
    assert baseline["content_policy"]["memory_body_included"] is False
