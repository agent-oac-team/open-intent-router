import pytest

from app.core.config import Settings
from app.db.session import create_all_tables, create_session_factory
from app.repositories.knowledge_assets import (
    DatabaseCanonicalKnowledgeRepository,
    MemoryCanonicalKnowledgeRepository,
)
from app.schemas.common import UserContext
from app.schemas.knowledge_assets import (
    CanonicalKnowledgeSearchRequest,
    ExactReadRequest,
    ExactReadTarget,
    GroupedKnowledgeSearchRequest,
    KnowledgeAccessPolicy,
    KnowledgeAsset,
    KnowledgeAssetChunk,
    KnowledgeAssetGroup,
    KnowledgeAssetStatus,
    KnowledgeMigrationManifest,
    KnowledgeSourceRef,
)
from app.services.knowledge_asset_service import KnowledgeAssetError, KnowledgeAssetService


@pytest.fixture(params=["memory", "database"])
async def knowledge_assets(request, tmp_path):
    if request.param == "memory":
        repository = MemoryCanonicalKnowledgeRepository()
    else:
        settings = Settings(
            storage_backend="database",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'knowledge-assets.db'}",
        )
        await create_all_tables(settings)
        repository = DatabaseCanonicalKnowledgeRepository(create_session_factory(settings))
    return repository, KnowledgeAssetService(repository, max_file_bytes=1024)


def _user(*, groups=None, tenant="tenant-1") -> UserContext:
    return UserContext(
        id="user-1",
        groups=groups or [],
        attributes={"tenant_id": tenant},
    )


async def _seed(repository):
    group = await repository.save_group(
        KnowledgeAssetGroup(
            group_id="group-1",
            tenant_id="tenant-1",
            name="content",
            stable_asset_keys=["01", "02", "03"],
        )
    )
    public = await repository.save_asset(
        KnowledgeAsset(
            asset_id="asset-1",
            tenant_id="tenant-1",
            owner_id="owner-1",
            stable_key="01",
            group_id=group.group_id,
            name="Public",
            status=KnowledgeAssetStatus.INDEXED,
        )
    )
    restricted = await repository.save_asset(
        KnowledgeAsset(
            asset_id="asset-2",
            tenant_id="tenant-1",
            owner_id="owner-1",
            stable_key="02",
            group_id=group.group_id,
            name="Restricted",
            status=KnowledgeAssetStatus.INDEXED,
            sensitivity="restricted",
            access_policy=KnowledgeAccessPolicy(allow_groups=["admin"]),
        )
    )
    await repository.save_chunk(
        KnowledgeAssetChunk(
            chunk_id="chunk-1",
            asset_id=public.asset_id,
            tenant_id="tenant-1",
            ordinal=0,
            content="存款活动期限三个月",
            content_hash="hash-1",
            source_ref=KnowledgeSourceRef(file_name="01.xlsx", sheet="Sheet1", row_start=2),
        )
    )
    await repository.save_chunk(
        KnowledgeAssetChunk(
            chunk_id="chunk-2",
            asset_id=restricted.asset_id,
            tenant_id="tenant-1",
            ordinal=0,
            content="内部客户名单",
            content_hash="hash-2",
            sensitivity="restricted",
        )
    )
    return group, public, restricted


async def test_search_filters_permissions_and_preserves_provenance(knowledge_assets) -> None:
    repository, service = knowledge_assets
    await _seed(repository)
    response = await service.search(
        CanonicalKnowledgeSearchRequest(
            query="存款活动期限",
            user=_user(),
            caller="coze",
            purpose="answer",
            tenant_id="tenant-1",
        )
    )
    denied = await service.search(
        CanonicalKnowledgeSearchRequest(
            query="内部客户名单",
            user=_user(),
            caller="host",
            purpose="answer",
            tenant_id="tenant-1",
        )
    )

    assert response.matched is True
    assert response.evidence[0].source_ref.file_name == "01.xlsx"
    assert denied.matched is False
    traces = await repository.list_traces(tenant_id="tenant-1")
    assert traces[0].evidence_ids == ["chunk-1"]
    assert traces[0].metadata["query_length"] == len("存款活动期限")
    assert "存款活动期限" not in traces[0].model_dump_json()


async def test_grouped_search_has_stable_order_and_empty_slot(knowledge_assets) -> None:
    repository, service = knowledge_assets
    await _seed(repository)
    response = await service.grouped_search(
        GroupedKnowledgeSearchRequest(
            query="存款活动",
            user=_user(groups=["admin"]),
            caller="host",
            purpose="answer",
            tenant_id="tenant-1",
            group_id="group-1",
            include_empty_assets=True,
        )
    )

    assert [item.stable_key for item in response.assets] == ["01", "02", "03"]
    assert response.assets[2].matched is False
    assert response.assets[2].warnings == [{"code": "asset_empty_or_missing"}]


async def test_exact_read_preserves_batch_order_and_reports_missing_filtered_ambiguous(
    knowledge_assets,
) -> None:
    repository, service = knowledge_assets
    await _seed(repository)
    response = await service.exact_read(
        ExactReadRequest(
            tenant_id="tenant-1",
            user=_user(),
            target=ExactReadTarget(type="chunks", chunk_ids=["chunk-2", "missing", "chunk-1"]),
        )
    )
    assert [chunk.chunk_id for chunk in response.chunks] == ["chunk-1"]
    assert response.missing == ["missing"]
    assert response.filtered == ["chunk-2"]

    await repository.save_chunk(
        KnowledgeAssetChunk(
            chunk_id="chunk-3",
            asset_id="asset-1",
            tenant_id="tenant-1",
            ordinal=1,
            content="another",
            content_hash="hash-3",
            source_ref=KnowledgeSourceRef(file_name="01.xlsx", sheet="Sheet1", row_start=2),
        )
    )
    ambiguous = await service.exact_read(
        ExactReadRequest(
            tenant_id="tenant-1",
            user=_user(),
            target=ExactReadTarget(
                type="source_ref",
                asset_id="asset-1",
                source_ref=KnowledgeSourceRef(file_name="01.xlsx", sheet="Sheet1", row_start=2),
            ),
        )
    )
    assert ambiguous.warnings[0]["code"] == "ambiguous_source_ref"


async def test_ingestion_state_replace_retry_soft_delete_and_validation(knowledge_assets) -> None:
    repository, service = knowledge_assets
    asset, job = await service.ingest_bytes(
        tenant_id="tenant-1",
        owner_id="owner-1",
        file_name="notes.txt",
        content_type="text/plain",
        data="第一条知识".encode(),
        stable_key="notes",
    )
    assert asset.status == KnowledgeAssetStatus.INDEXED
    assert job.status == "completed"
    assert [stage.value for stage in job.stage_history] == [
        "upload",
        "validation",
        "parsing",
        "chunking",
        "embedding",
        "indexing",
        "completed",
    ]
    assert len(await repository.get_chunks(tenant_id="tenant-1", asset_ids=[asset.asset_id])) == 1

    replaced, _ = await service.ingest_bytes(
        tenant_id="tenant-1",
        owner_id="owner-1",
        file_name="notes.txt",
        content_type="text/plain",
        data="替换后的知识".encode(),
        replace_asset_id=asset.asset_id,
    )
    chunks = await repository.get_chunks(tenant_id="tenant-1", asset_ids=[asset.asset_id])
    assert replaced.asset_id == asset.asset_id
    assert [chunk.content for chunk in chunks] == ["替换后的知识"]

    preserved, failed_replace_job = await service.ingest_bytes(
        tenant_id="tenant-1",
        owner_id="owner-1",
        file_name="notes.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=b"invalid replacement",
        replace_asset_id=asset.asset_id,
    )
    preserved_chunks = await repository.get_chunks(tenant_id="tenant-1", asset_ids=[asset.asset_id])
    assert failed_replace_job.status == "failed"
    assert preserved.status == KnowledgeAssetStatus.INDEXED
    assert [chunk.content for chunk in preserved_chunks] == ["替换后的知识"]

    failed, failed_job = await service.ingest_bytes(
        tenant_id="tenant-1",
        owner_id="owner-1",
        file_name="book.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=b"not-yet-parsed",
    )
    assert failed.status == KnowledgeAssetStatus.FAILED
    retried, retried_job = await service.retry_job(
        failed_job.job_id,
        extracted_chunks=[("parsed row", KnowledgeSourceRef(file_name="book.xlsx", row_start=2))],
    )
    assert retried.status == KnowledgeAssetStatus.INDEXED
    assert retried_job.attempt == 2

    deleted = await service.soft_delete(asset.asset_id, tenant_id="tenant-1", owner_id="owner-1")
    assert deleted.status == KnowledgeAssetStatus.DELETED
    search = await service.search(
        CanonicalKnowledgeSearchRequest(
            query="替换后的知识",
            user=_user(),
            caller="host",
            purpose="answer",
            tenant_id="tenant-1",
        )
    )
    assert search.matched is False

    for file_name, content_type, data in [
        ("../secret.txt", "text/plain", b"x"),
        ("secret.exe", "text/plain", b"x"),
        ("secret.txt", "application/octet-stream", b"x"),
        ("secret.txt", "text/plain", b"x" * 1025),
    ]:
        with pytest.raises(KnowledgeAssetError):
            await service.ingest_bytes(
                tenant_id="tenant-1",
                owner_id="owner-1",
                file_name=file_name,
                content_type=content_type,
                data=data,
            )


async def test_manifest_status_can_advance_after_vector_and_golden_validation(
    knowledge_assets,
) -> None:
    repository, _ = knowledge_assets
    manifest = KnowledgeMigrationManifest(
        manifest_id="manifest-1",
        tenant_id="tenant-1",
        source_path="01.xlsx",
        file_hash="file-hash",
        pipeline_version="pipeline-v1",
        asset_id="asset-1",
        migration_status="parsed",
        validation_status="pending",
    )
    await repository.save_manifest(manifest)

    await repository.replace_manifest(
        manifest.model_copy(
            update={
                "migration_status": "indexed",
                "validation_status": "validated",
                "metadata": {"vector_count": 1, "legacy_vectors_copied": False},
            }
        )
    )
    stored = await repository.find_manifest(
        tenant_id="tenant-1",
        file_hash="file-hash",
        pipeline_version="pipeline-v1",
    )

    assert stored is not None
    assert stored.migration_status == "indexed"
    assert stored.validation_status == "validated"
    assert stored.metadata == {"vector_count": 1, "legacy_vectors_copied": False}
