import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.session import create_session_factory  # noqa: E402
from app.repositories.context_stores import KnowledgeRepository  # noqa: E402
from app.repositories.knowledge_assets import (  # noqa: E402
    DatabaseCanonicalKnowledgeRepository,
)
from app.schemas.knowledge import KnowledgeChunk  # noqa: E402
from app.services.knowledge_vector_store import MilvusKnowledgeVectorStore  # noqa: E402


async def reindex_canonical_knowledge(
    *, settings: Settings, recreate: bool, batch_size: int = 32
) -> dict:
    if settings.knowledge_milvus_collection != "oir_knowledge_vectors":
        raise ValueError("Canonical Knowledge reindex must target oir_knowledge_vectors")
    if settings.knowledge_milvus_collection == settings.memory_milvus_collection:
        raise ValueError("Knowledge and Memory Milvus collections must be isolated")
    repository = DatabaseCanonicalKnowledgeRepository(create_session_factory(settings))
    assets = await repository.list_assets(tenant_id="oac", group_id="content_production")
    assets.sort(key=lambda asset: asset.stable_key or "")
    canonical_chunks = await repository.get_chunks(
        tenant_id="oac", asset_ids=[asset.asset_id for asset in assets]
    )
    vector_chunks = [
        KnowledgeChunk(
            chunk_id=chunk.chunk_id,
            source_id=chunk.asset_id,
            content=chunk.content,
            title=chunk.title,
            uri=_source_ref_uri(chunk.source_ref),
            metadata={
                "tenant_id": chunk.tenant_id,
                "asset_id": chunk.asset_id,
                "content_hash": chunk.content_hash,
            },
            updated_at=chunk.updated_at,
        )
        for chunk in canonical_chunks
        if chunk.status == "active"
    ]
    vector_store = MilvusKnowledgeVectorStore(settings, KnowledgeRepository())
    if recreate:
        vector_store.reset_collection()
    indexed_count = 0
    for offset in range(0, len(vector_chunks), batch_size):
        indexed_count += await vector_store.upsert_chunks(
            vector_chunks[offset : offset + batch_size]
        )
    records = vector_store.list_index_records()
    expected_ids = {chunk.chunk_id for chunk in vector_chunks}
    records_by_id = {
        str(record["chunk_id"]): record
        for record in records
        if isinstance(record.get("chunk_id"), str)
    }
    indexed_ids = set(records_by_id)
    expected_by_asset = Counter(chunk.source_id for chunk in vector_chunks)
    indexed_by_asset = Counter(
        str(record["source_id"])
        for record in records_by_id.values()
        if isinstance(record.get("source_id"), str)
    )
    missing = sorted(expected_ids - indexed_ids)
    extra = sorted(indexed_ids - expected_ids)
    passed = (
        indexed_count == len(vector_chunks)
        and expected_by_asset == indexed_by_asset
        and not missing
        and not extra
    )
    if passed:
        for asset in assets:
            manifest = await repository.find_manifest(
                tenant_id="oac",
                file_hash=asset.file_hash or "",
                pipeline_version=str(asset.metadata.get("pipeline_version", "")),
            )
            if manifest is None:
                raise RuntimeError(f"Missing migration manifest for {asset.asset_id}")
            if asset.stable_key == "03":
                continue
            await repository.replace_manifest(
                manifest.model_copy(
                    update={
                        "migration_status": "indexed",
                        "validation_status": "pending",
                        "metadata": {
                            **manifest.metadata,
                            "vector_source": "oir_canonical_chunks",
                            "vector_count": indexed_by_asset[asset.asset_id],
                            "legacy_vectors_copied": False,
                        },
                    }
                )
            )
    return {
        "contract": "oir-canonical-knowledge-reindex/v1",
        "collection": settings.knowledge_milvus_collection,
        "recreated": recreate,
        "vector_source": "oir_canonical_chunks",
        "legacy_vectors_copied": False,
        "canonical_chunk_count": len(vector_chunks),
        "upserted_count": indexed_count,
        "raw_query_record_count": len(records),
        "indexed_record_count": len(indexed_ids),
        "duplicate_query_record_count": len(records) - len(indexed_ids),
        "expected_by_asset": dict(sorted(expected_by_asset.items())),
        "indexed_by_asset": dict(sorted(indexed_by_asset.items())),
        "missing_chunk_ids": missing,
        "extra_chunk_ids": extra,
        "passed": passed,
    }


def _source_ref_uri(source_ref) -> str:
    location = source_ref.cell_range or (
        f"rows:{source_ref.row_start}-{source_ref.row_end or source_ref.row_start}"
        if source_ref.row_start
        else ""
    )
    parts = [source_ref.file_name or "", source_ref.sheet or "", location]
    return "#".join(part for part in parts if part)


def main() -> None:
    parser = argparse.ArgumentParser()
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--database-url")
    database.add_argument("--use-configured-database", action="store_true")
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    settings = (
        Settings(storage_backend="database")
        if args.use_configured_database
        else Settings(
            storage_backend="database",
            database_url=args.database_url or "sqlite+aiosqlite:///./data/oir-migration.db",
        )
    )
    report = asyncio.run(
        reindex_canonical_knowledge(
            settings=settings,
            recreate=args.recreate,
            batch_size=args.batch_size,
        )
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
