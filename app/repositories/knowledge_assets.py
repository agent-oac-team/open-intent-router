import asyncio
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    KnowledgeAssetChunkModel,
    KnowledgeAssetGroupModel,
    KnowledgeAssetModel,
    KnowledgeImportJobModel,
    KnowledgeMigrationManifestModel,
    KnowledgeOperationTraceModel,
)
from app.repositories.json_utils import dumps, loads
from app.schemas.knowledge_assets import (
    KnowledgeAsset,
    KnowledgeAssetChunk,
    KnowledgeAssetGroup,
    KnowledgeImportJob,
    KnowledgeMigrationManifest,
    KnowledgeOperationTrace,
)


class CanonicalKnowledgeRepository(Protocol):
    async def save_asset(self, asset: KnowledgeAsset) -> KnowledgeAsset: ...

    async def get_asset(self, asset_id: str) -> KnowledgeAsset | None: ...

    async def list_assets(
        self, *, tenant_id: str, asset_ids: list[str] | None = None, group_id: str | None = None
    ) -> list[KnowledgeAsset]: ...

    async def save_chunk(self, chunk: KnowledgeAssetChunk) -> KnowledgeAssetChunk: ...

    async def get_chunks(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        asset_ids: list[str] | None = None,
    ) -> list[KnowledgeAssetChunk]: ...

    async def replace_chunks(
        self, asset_id: str, chunks: list[KnowledgeAssetChunk]
    ) -> list[KnowledgeAssetChunk]: ...

    async def save_group(self, group: KnowledgeAssetGroup) -> KnowledgeAssetGroup: ...

    async def get_group(self, group_id: str) -> KnowledgeAssetGroup | None: ...

    async def save_job(self, job: KnowledgeImportJob) -> KnowledgeImportJob: ...

    async def get_job(self, job_id: str) -> KnowledgeImportJob | None: ...

    async def list_jobs(self, *, asset_id: str) -> list[KnowledgeImportJob]: ...

    async def save_manifest(
        self, manifest: KnowledgeMigrationManifest
    ) -> KnowledgeMigrationManifest: ...

    async def replace_manifest(
        self, manifest: KnowledgeMigrationManifest
    ) -> KnowledgeMigrationManifest: ...

    async def find_manifest(
        self, *, tenant_id: str, file_hash: str, pipeline_version: str
    ) -> KnowledgeMigrationManifest | None: ...

    async def save_trace(self, trace: KnowledgeOperationTrace) -> KnowledgeOperationTrace: ...

    async def list_traces(self, *, tenant_id: str) -> list[KnowledgeOperationTrace]: ...


class MemoryCanonicalKnowledgeRepository:
    def __init__(self) -> None:
        self.assets: dict[str, KnowledgeAsset] = {}
        self.chunks: dict[str, KnowledgeAssetChunk] = {}
        self.groups: dict[str, KnowledgeAssetGroup] = {}
        self.jobs: dict[str, KnowledgeImportJob] = {}
        self.manifests: dict[str, KnowledgeMigrationManifest] = {}
        self.traces: dict[str, KnowledgeOperationTrace] = {}
        self._lock = asyncio.Lock()

    async def save_asset(self, asset: KnowledgeAsset) -> KnowledgeAsset:
        async with self._lock:
            self.assets[asset.asset_id] = asset
        return asset.model_copy(deep=True)

    async def get_asset(self, asset_id: str) -> KnowledgeAsset | None:
        asset = self.assets.get(asset_id)
        return asset.model_copy(deep=True) if asset else None

    async def list_assets(
        self, *, tenant_id: str, asset_ids: list[str] | None = None, group_id: str | None = None
    ) -> list[KnowledgeAsset]:
        requested = set(asset_ids or [])
        return [
            asset.model_copy(deep=True)
            for asset in self.assets.values()
            if asset.tenant_id == tenant_id
            and (not requested or asset.asset_id in requested)
            and (group_id is None or asset.group_id == group_id)
        ]

    async def save_chunk(self, chunk: KnowledgeAssetChunk) -> KnowledgeAssetChunk:
        async with self._lock:
            self.chunks[chunk.chunk_id] = chunk
        return chunk.model_copy(deep=True)

    async def get_chunks(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        asset_ids: list[str] | None = None,
    ) -> list[KnowledgeAssetChunk]:
        requested_chunks = set(chunk_ids or [])
        requested_assets = set(asset_ids or [])
        chunks = [
            chunk.model_copy(deep=True)
            for chunk in self.chunks.values()
            if chunk.tenant_id == tenant_id
            and (not requested_chunks or chunk.chunk_id in requested_chunks)
            and (not requested_assets or chunk.asset_id in requested_assets)
        ]
        return sorted(chunks, key=lambda item: (item.asset_id, item.ordinal))

    async def replace_chunks(
        self, asset_id: str, chunks: list[KnowledgeAssetChunk]
    ) -> list[KnowledgeAssetChunk]:
        async with self._lock:
            self.chunks = {
                key: value for key, value in self.chunks.items() if value.asset_id != asset_id
            }
            self.chunks.update({chunk.chunk_id: chunk for chunk in chunks})
        return [chunk.model_copy(deep=True) for chunk in chunks]

    async def save_group(self, group: KnowledgeAssetGroup) -> KnowledgeAssetGroup:
        self.groups[group.group_id] = group
        return group.model_copy(deep=True)

    async def get_group(self, group_id: str) -> KnowledgeAssetGroup | None:
        group = self.groups.get(group_id)
        return group.model_copy(deep=True) if group else None

    async def save_job(self, job: KnowledgeImportJob) -> KnowledgeImportJob:
        self.jobs[job.job_id] = job
        return job.model_copy(deep=True)

    async def get_job(self, job_id: str) -> KnowledgeImportJob | None:
        job = self.jobs.get(job_id)
        return job.model_copy(deep=True) if job else None

    async def list_jobs(self, *, asset_id: str) -> list[KnowledgeImportJob]:
        return sorted(
            [job.model_copy(deep=True) for job in self.jobs.values() if job.asset_id == asset_id],
            key=lambda job: job.created_at,
            reverse=True,
        )

    async def save_manifest(
        self, manifest: KnowledgeMigrationManifest
    ) -> KnowledgeMigrationManifest:
        existing = await self.find_manifest(
            tenant_id=manifest.tenant_id,
            file_hash=manifest.file_hash,
            pipeline_version=manifest.pipeline_version,
        )
        if existing:
            return existing
        self.manifests[manifest.manifest_id] = manifest
        return manifest.model_copy(deep=True)

    async def replace_manifest(
        self, manifest: KnowledgeMigrationManifest
    ) -> KnowledgeMigrationManifest:
        self.manifests[manifest.manifest_id] = manifest
        return manifest.model_copy(deep=True)

    async def find_manifest(
        self, *, tenant_id: str, file_hash: str, pipeline_version: str
    ) -> KnowledgeMigrationManifest | None:
        return next(
            (
                manifest.model_copy(deep=True)
                for manifest in self.manifests.values()
                if manifest.tenant_id == tenant_id
                and manifest.file_hash == file_hash
                and manifest.pipeline_version == pipeline_version
            ),
            None,
        )

    async def save_trace(self, trace: KnowledgeOperationTrace) -> KnowledgeOperationTrace:
        self.traces[trace.trace_id] = trace
        return trace.model_copy(deep=True)

    async def list_traces(self, *, tenant_id: str) -> list[KnowledgeOperationTrace]:
        return [trace for trace in self.traces.values() if trace.tenant_id == tenant_id]


class DatabaseCanonicalKnowledgeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def save_asset(self, asset: KnowledgeAsset) -> KnowledgeAsset:
        async with self.session_factory() as session, session.begin():
            row = await session.get(KnowledgeAssetModel, asset.asset_id)
            values = _asset_values(asset)
            if row is None:
                row = KnowledgeAssetModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.flush()
            await session.refresh(row)
            return _asset_from_row(row)

    async def get_asset(self, asset_id: str) -> KnowledgeAsset | None:
        async with self.session_factory() as session:
            row = await session.get(KnowledgeAssetModel, asset_id)
            return _asset_from_row(row) if row else None

    async def list_assets(
        self, *, tenant_id: str, asset_ids: list[str] | None = None, group_id: str | None = None
    ) -> list[KnowledgeAsset]:
        async with self.session_factory() as session:
            stmt = select(KnowledgeAssetModel).where(KnowledgeAssetModel.tenant_id == tenant_id)
            if asset_ids:
                stmt = stmt.where(KnowledgeAssetModel.asset_id.in_(asset_ids))
            if group_id:
                stmt = stmt.where(KnowledgeAssetModel.group_id == group_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_asset_from_row(row) for row in rows]

    async def save_chunk(self, chunk: KnowledgeAssetChunk) -> KnowledgeAssetChunk:
        async with self.session_factory() as session, session.begin():
            row = await session.get(KnowledgeAssetChunkModel, chunk.chunk_id)
            values = _chunk_values(chunk)
            if row is None:
                row = KnowledgeAssetChunkModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.flush()
            await session.refresh(row)
            return _chunk_from_row(row)

    async def get_chunks(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        asset_ids: list[str] | None = None,
    ) -> list[KnowledgeAssetChunk]:
        async with self.session_factory() as session:
            stmt = select(KnowledgeAssetChunkModel).where(
                KnowledgeAssetChunkModel.tenant_id == tenant_id
            )
            if chunk_ids:
                stmt = stmt.where(KnowledgeAssetChunkModel.chunk_id.in_(chunk_ids))
            if asset_ids:
                stmt = stmt.where(KnowledgeAssetChunkModel.asset_id.in_(asset_ids))
            stmt = stmt.order_by(
                KnowledgeAssetChunkModel.asset_id, KnowledgeAssetChunkModel.ordinal
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_chunk_from_row(row) for row in rows]

    async def replace_chunks(
        self, asset_id: str, chunks: list[KnowledgeAssetChunk]
    ) -> list[KnowledgeAssetChunk]:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                delete(KnowledgeAssetChunkModel).where(
                    KnowledgeAssetChunkModel.asset_id == asset_id
                )
            )
            await session.flush()
            session.add_all([KnowledgeAssetChunkModel(**_chunk_values(chunk)) for chunk in chunks])
        return chunks

    async def save_group(self, group: KnowledgeAssetGroup) -> KnowledgeAssetGroup:
        async with self.session_factory() as session, session.begin():
            row = await session.get(KnowledgeAssetGroupModel, group.group_id)
            values = {
                "group_id": group.group_id,
                "tenant_id": group.tenant_id,
                "name": group.name,
                "stable_asset_keys_text": dumps(group.stable_asset_keys),
                "metadata_text": dumps(group.metadata),
            }
            if row is None:
                row = KnowledgeAssetGroupModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.flush()
            return _group_from_row(row)

    async def get_group(self, group_id: str) -> KnowledgeAssetGroup | None:
        async with self.session_factory() as session:
            row = await session.get(KnowledgeAssetGroupModel, group_id)
            return _group_from_row(row) if row else None

    async def save_job(self, job: KnowledgeImportJob) -> KnowledgeImportJob:
        async with self.session_factory() as session, session.begin():
            row = await session.get(KnowledgeImportJobModel, job.job_id)
            values = _job_values(job)
            if row is None:
                row = KnowledgeImportJobModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.flush()
            await session.refresh(row)
            return _job_from_row(row)

    async def get_job(self, job_id: str) -> KnowledgeImportJob | None:
        async with self.session_factory() as session:
            row = await session.get(KnowledgeImportJobModel, job_id)
            return _job_from_row(row) if row else None

    async def list_jobs(self, *, asset_id: str) -> list[KnowledgeImportJob]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeImportJobModel)
                        .where(KnowledgeImportJobModel.asset_id == asset_id)
                        .order_by(KnowledgeImportJobModel.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_job_from_row(row) for row in rows]

    async def save_manifest(
        self, manifest: KnowledgeMigrationManifest
    ) -> KnowledgeMigrationManifest:
        existing = await self.find_manifest(
            tenant_id=manifest.tenant_id,
            file_hash=manifest.file_hash,
            pipeline_version=manifest.pipeline_version,
        )
        if existing:
            return existing
        async with self.session_factory() as session, session.begin():
            row = KnowledgeMigrationManifestModel(**_manifest_values(manifest))
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _manifest_from_row(row)

    async def replace_manifest(
        self, manifest: KnowledgeMigrationManifest
    ) -> KnowledgeMigrationManifest:
        async with self.session_factory() as session, session.begin():
            row = await session.get(KnowledgeMigrationManifestModel, manifest.manifest_id)
            values = _manifest_values(manifest)
            if row is None:
                row = KnowledgeMigrationManifestModel(**values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.flush()
            await session.refresh(row)
            return _manifest_from_row(row)

    async def find_manifest(
        self, *, tenant_id: str, file_hash: str, pipeline_version: str
    ) -> KnowledgeMigrationManifest | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(KnowledgeMigrationManifestModel).where(
                    KnowledgeMigrationManifestModel.tenant_id == tenant_id,
                    KnowledgeMigrationManifestModel.file_hash == file_hash,
                    KnowledgeMigrationManifestModel.pipeline_version == pipeline_version,
                )
            )
            return _manifest_from_row(row) if row else None

    async def save_trace(self, trace: KnowledgeOperationTrace) -> KnowledgeOperationTrace:
        async with self.session_factory() as session, session.begin():
            session.add(
                KnowledgeOperationTraceModel(
                    trace_id=trace.trace_id,
                    tenant_id=trace.tenant_id,
                    user_id=trace.user_id,
                    operation=trace.operation,
                    caller=trace.caller,
                    purpose=trace.purpose,
                    policy_outcome=trace.policy_outcome,
                    evidence_ids_text=dumps(trace.evidence_ids),
                    warnings_text=dumps(trace.warnings),
                    latency_ms=trace.latency_ms,
                    metadata_text=dumps(trace.metadata),
                    created_at=trace.created_at,
                )
            )
        return trace

    async def list_traces(self, *, tenant_id: str) -> list[KnowledgeOperationTrace]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeOperationTraceModel).where(
                            KnowledgeOperationTraceModel.tenant_id == tenant_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [
                KnowledgeOperationTrace(
                    trace_id=row.trace_id,
                    tenant_id=row.tenant_id,
                    user_id=row.user_id,
                    operation=row.operation,
                    caller=row.caller,
                    purpose=row.purpose,
                    policy_outcome=row.policy_outcome,
                    evidence_ids=loads(row.evidence_ids_text, []),
                    warnings=loads(row.warnings_text, []),
                    latency_ms=row.latency_ms,
                    metadata=loads(row.metadata_text, {}),
                    created_at=_utc(row.created_at),
                )
                for row in rows
            ]


def _asset_values(asset: KnowledgeAsset) -> dict:
    data = asset.model_dump(exclude={"access_policy", "tags", "metadata"})
    data["status"] = asset.status.value
    data["access_policy_text"] = dumps(asset.access_policy.model_dump())
    data["tags_text"] = dumps(asset.tags)
    data["metadata_text"] = dumps(asset.metadata)
    return data


def _asset_from_row(row: KnowledgeAssetModel) -> KnowledgeAsset:
    return KnowledgeAsset(
        asset_id=row.asset_id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        stable_key=row.stable_key,
        group_id=row.group_id,
        name=row.name,
        description=row.description,
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        file_hash=row.file_hash,
        content_hash=row.content_hash,
        status=row.status,
        sensitivity=row.sensitivity,
        access_policy=loads(row.access_policy_text, {}),
        tags=loads(row.tags_text, []),
        parser_version=row.parser_version,
        chunking_version=row.chunking_version,
        embedding_version=row.embedding_version,
        metadata=loads(row.metadata_text, {}),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        deleted_at=_utc(row.deleted_at),
    )


def _chunk_values(chunk: KnowledgeAssetChunk) -> dict:
    data = chunk.model_dump(exclude={"source_ref", "citation", "metadata"})
    data["source_ref_text"] = dumps(chunk.source_ref.model_dump())
    data["citation_text"] = dumps(chunk.citation)
    data["metadata_text"] = dumps(chunk.metadata)
    return data


def _chunk_from_row(row: KnowledgeAssetChunkModel) -> KnowledgeAssetChunk:
    return KnowledgeAssetChunk(
        chunk_id=row.chunk_id,
        asset_id=row.asset_id,
        tenant_id=row.tenant_id,
        ordinal=row.ordinal,
        content=row.content,
        content_hash=row.content_hash,
        title=row.title,
        source_ref=loads(row.source_ref_text, {}),
        citation=loads(row.citation_text, {}),
        status=row.status,
        sensitivity=row.sensitivity,
        metadata=loads(row.metadata_text, {}),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _group_from_row(row: KnowledgeAssetGroupModel) -> KnowledgeAssetGroup:
    return KnowledgeAssetGroup(
        group_id=row.group_id,
        tenant_id=row.tenant_id,
        name=row.name,
        stable_asset_keys=loads(row.stable_asset_keys_text, []),
        metadata=loads(row.metadata_text, {}),
    )


def _job_values(job: KnowledgeImportJob) -> dict:
    data = job.model_dump(exclude={"stage_history", "warnings", "error"})
    data["status"] = job.status
    data["stage"] = job.stage.value
    data["stage_history_text"] = dumps([stage.value for stage in job.stage_history])
    data["warnings_text"] = dumps(job.warnings)
    data["error_text"] = dumps(job.error) if job.error else None
    return data


def _job_from_row(row: KnowledgeImportJobModel) -> KnowledgeImportJob:
    return KnowledgeImportJob(
        job_id=row.job_id,
        tenant_id=row.tenant_id,
        asset_id=row.asset_id,
        status=row.status,
        stage=row.stage,
        stage_history=loads(row.stage_history_text, []),
        attempt=row.attempt,
        warnings=loads(row.warnings_text, []),
        error=loads(row.error_text, None),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        completed_at=_utc(row.completed_at),
    )


def _manifest_values(manifest: KnowledgeMigrationManifest) -> dict:
    data = manifest.model_dump(exclude={"chunk_ids", "source_refs", "metadata"})
    data["chunk_ids_text"] = dumps(manifest.chunk_ids)
    data["source_refs_text"] = dumps(
        [source_ref.model_dump() for source_ref in manifest.source_refs]
    )
    data["metadata_text"] = dumps(manifest.metadata)
    return data


def _manifest_from_row(row: KnowledgeMigrationManifestModel) -> KnowledgeMigrationManifest:
    return KnowledgeMigrationManifest(
        manifest_id=row.manifest_id,
        tenant_id=row.tenant_id,
        source_path=row.source_path,
        file_hash=row.file_hash,
        pipeline_version=row.pipeline_version,
        asset_id=row.asset_id,
        chunk_ids=loads(row.chunk_ids_text, []),
        collection_name=row.collection_name,
        migration_status=row.migration_status,
        validation_status=row.validation_status,
        source_refs=loads(row.source_refs_text, []),
        metadata=loads(row.metadata_text, {}),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
