import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.session import create_all_tables, create_session_factory  # noqa: E402
from app.repositories.knowledge_assets import (  # noqa: E402
    DatabaseCanonicalKnowledgeRepository,
)
from app.schemas.knowledge_assets import (  # noqa: E402
    KnowledgeAssetChunk,
    KnowledgeAssetGroup,
    KnowledgeMigrationManifest,
)
from app.services.knowledge_asset_service import KnowledgeAssetService  # noqa: E402
from app.services.knowledge_excel_parser import (  # noqa: E402
    CHUNKING_VERSION,
    PARSER_VERSION,
    parse_workbook,
)

SOURCE_ROOT = Path("/Users/lijingtong/project/data/内容生产")
PIPELINE_VERSION = f"{PARSER_VERSION}+{CHUNKING_VERSION}+embedding-config-v1"


async def import_workbooks(*, source_root: Path, settings: Settings, dry_run: bool) -> dict:
    files = sorted(source_root.glob("[0-9][0-9] *.xlsx"))
    if len(files) != 6:
        raise ValueError(f"Expected 6 content workbooks, found {len(files)}")
    if settings.knowledge_milvus_collection == settings.memory_milvus_collection:
        raise ValueError("Knowledge and Memory Milvus collections must be isolated")
    if settings.knowledge_milvus_collection != "oir_knowledge_vectors":
        raise ValueError("Knowledge migration must target oir_knowledge_vectors")
    _validate_database_target(settings.database_url)
    if not dry_run:
        await create_all_tables(settings)
    repository = DatabaseCanonicalKnowledgeRepository(create_session_factory(settings))
    service = KnowledgeAssetService(repository)
    if not dry_run:
        await service.save_group(
            KnowledgeAssetGroup(
                group_id="content_production",
                tenant_id="oac",
                name="content_production",
                stable_asset_keys=["01", "02", "03", "04", "05", "06"],
                metadata={"source_root": str(source_root)},
            )
        )
    results = []
    for path in files:
        parsed = parse_workbook(path)
        existing = None
        if not dry_run:
            existing = await repository.find_manifest(
                tenant_id="oac",
                file_hash=parsed.file_hash,
                pipeline_version=PIPELINE_VERSION,
            )
        if existing:
            results.append(
                {
                    "stable_key": parsed.stable_key,
                    "file": path.name,
                    "status": "idempotent_existing",
                    "asset_id": existing.asset_id,
                    "chunk_count": len(existing.chunk_ids),
                    "manifest_id": existing.manifest_id,
                }
            )
            continue
        stable_chunks = [
            KnowledgeAssetChunk(
                chunk_id=chunk.chunk_id,
                asset_id=parsed.asset_id,
                tenant_id="oac",
                ordinal=index,
                content=chunk.content,
                content_hash=chunk.content_hash,
                title=chunk.title,
                source_ref=chunk.source_ref,
                citation={
                    "asset_id": parsed.asset_id,
                    "chunk_id": chunk.chunk_id,
                    "source_ref": chunk.source_ref.model_dump(mode="json"),
                    "business_domain": "内容生产",
                    "sensitivity": "internal",
                    "structured_payload": chunk.structured_payload,
                },
                metadata={
                    "business_domain": "内容生产",
                    "structured_payload": chunk.structured_payload,
                },
            )
            for index, chunk in enumerate(parsed.chunks)
        ]
        if not dry_run:
            asset, job = await service.ingest_bytes(
                tenant_id="oac",
                owner_id="migration",
                file_name=path.name,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                data=path.read_bytes(),
                name=parsed.asset_name,
                asset_id=parsed.asset_id,
                stable_key=parsed.stable_key,
                group_id="content_production",
                extracted_chunks=[(chunk.content, chunk.source_ref) for chunk in parsed.chunks],
                metadata={
                    "business_domain": "内容生产",
                    "source_type": "excel",
                    "source_name": f"{parsed.asset_name} 表",
                    "pipeline_version": PIPELINE_VERSION,
                },
            )
            await repository.replace_chunks(asset.asset_id, stable_chunks)
            manifest = await repository.save_manifest(
                KnowledgeMigrationManifest(
                    manifest_id=f"manifest_content_production_{parsed.stable_key}",
                    tenant_id="oac",
                    source_path=str(path),
                    file_hash=parsed.file_hash,
                    pipeline_version=PIPELINE_VERSION,
                    asset_id=asset.asset_id,
                    chunk_ids=[chunk.chunk_id for chunk in stable_chunks],
                    collection_name=settings.knowledge_milvus_collection,
                    migration_status="deferred" if parsed.empty else "parsed",
                    validation_status="deferred" if parsed.empty else "pending",
                    source_refs=[chunk.source_ref for chunk in parsed.chunks],
                    metadata={
                        "job_id": job.job_id,
                        "vector_source": "oir_canonical_chunks",
                        "vector_count": 0,
                        "legacy_vectors_copied": False,
                        "parser_version": PARSER_VERSION,
                        "chunking_version": CHUNKING_VERSION,
                    },
                )
            )
            manifest_id = manifest.manifest_id
        else:
            manifest_id = f"manifest_content_production_{parsed.stable_key}"
        results.append(
            {
                "stable_key": parsed.stable_key,
                "file": path.name,
                "status": "deferred" if parsed.empty else "parsed",
                "asset_id": parsed.asset_id,
                "chunk_count": len(parsed.chunks),
                "manifest_id": manifest_id,
                "file_hash": parsed.file_hash,
            }
        )
    return {
        "contract": "oir-content-workbook-migration/v1",
        "database_url": _redact_database_url(settings.database_url),
        "knowledge_collection": settings.knowledge_milvus_collection,
        "memory_collection": settings.memory_milvus_collection,
        "legacy_vectors_copied": False,
        "pipeline_version": PIPELINE_VERSION,
        "files": results,
        "passed": len(results) == 6
        and all(item["chunk_count"] > 0 for item in results if item["stable_key"] != "03")
        and next(item for item in results if item["stable_key"] == "03")["chunk_count"] == 0,
    }


def _redact_database_url(url: str) -> str:
    return url.rsplit("@", 1)[-1] if "@" in url else url


def _validate_database_target(database_url: str) -> None:
    parsed = make_url(database_url)
    database = (parsed.database or "").lower()
    database_name = Path(database).name
    if database_name == "oac" or Path(database_name).stem == "oac":
        raise ValueError("Knowledge migration must not write the IRS/OAC database")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--database-url")
    database.add_argument("--use-configured-database", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.use_configured_database:
        settings = Settings(storage_backend="database")
    else:
        settings = Settings(
            storage_backend="database",
            database_url=args.database_url or "sqlite+aiosqlite:///./data/oir-migration.db",
            knowledge_milvus_collection="oir_knowledge_vectors",
            memory_milvus_collection="oir_memory_vectors",
        )
    report = asyncio.run(
        import_workbooks(source_root=args.source_root, settings=settings, dry_run=args.dry_run)
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
