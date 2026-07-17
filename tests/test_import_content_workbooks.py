from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from scripts.import_content_workbooks import import_workbooks

SOURCE_ROOT = Path("/Users/lijingtong/project/data/内容生产")


def test_settings_reject_shared_knowledge_and_memory_collection() -> None:
    with pytest.raises(ValidationError, match="Milvus collections must be distinct"):
        Settings(
            knowledge_milvus_collection="shared_vectors",
            memory_milvus_collection="shared_vectors",
        )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://oir@example.invalid/oac?ssl=require",
        "sqlite+aiosqlite:///tmp/oac.db",
    ],
)
async def test_importer_rejects_irs_or_oac_database(database_url: str) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=database_url,
        knowledge_milvus_collection="oir_knowledge_vectors",
        memory_milvus_collection="oir_memory_vectors",
    )

    with pytest.raises(ValueError, match="must not write the IRS/OAC database"):
        await import_workbooks(source_root=SOURCE_ROOT, settings=settings, dry_run=True)


async def test_importer_accepts_independent_oir_database(tmp_path: Path) -> None:
    settings = Settings(
        storage_backend="database",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'oir-knowledge.db'}",
        knowledge_milvus_collection="oir_knowledge_vectors",
        memory_milvus_collection="oir_memory_vectors",
    )

    report = await import_workbooks(source_root=SOURCE_ROOT, settings=settings, dry_run=True)

    assert report["passed"] is True
    assert report["knowledge_collection"] == "oir_knowledge_vectors"
    assert report["memory_collection"] == "oir_memory_vectors"
    assert report["legacy_vectors_copied"] is False
