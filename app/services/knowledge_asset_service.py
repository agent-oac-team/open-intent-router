import hashlib
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.repositories.knowledge_assets import CanonicalKnowledgeRepository
from app.schemas.common import UserContext
from app.schemas.knowledge_assets import (
    CanonicalKnowledgeSearchRequest,
    CanonicalKnowledgeSearchResponse,
    ExactReadRequest,
    ExactReadResponse,
    GroupedKnowledgeAssetResult,
    GroupedKnowledgeSearchRequest,
    GroupedKnowledgeSearchResponse,
    KnowledgeAccessPolicy,
    KnowledgeAsset,
    KnowledgeAssetChunk,
    KnowledgeAssetGroup,
    KnowledgeAssetStatus,
    KnowledgeEvidence,
    KnowledgeImportJob,
    KnowledgeIngestionStage,
    KnowledgeOperationTrace,
    KnowledgeSourceRef,
)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".csv", ".xlsx", ".xls", ".pdf"}
SUPPORTED_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class KnowledgeAssetError(ValueError):
    pass


class KnowledgeAssetService:
    def __init__(
        self,
        repository: CanonicalKnowledgeRepository,
        *,
        max_file_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.repository = repository
        self.max_file_bytes = max_file_bytes

    async def save_group(self, group: KnowledgeAssetGroup) -> KnowledgeAssetGroup:
        return await self.repository.save_group(group)

    async def get_asset(self, asset_id: str) -> KnowledgeAsset | None:
        return await self.repository.get_asset(asset_id)

    async def list_assets(self, *, tenant_id: str) -> list[KnowledgeAsset]:
        return await self.repository.list_assets(tenant_id=tenant_id)

    async def get_chunks(
        self,
        *,
        tenant_id: str,
        asset_ids: list[str] | None = None,
        chunk_ids: list[str] | None = None,
    ) -> list[KnowledgeAssetChunk]:
        return await self.repository.get_chunks(
            tenant_id=tenant_id, asset_ids=asset_ids, chunk_ids=chunk_ids
        )

    async def latest_job(self, asset_id: str) -> KnowledgeImportJob | None:
        jobs = await self.repository.list_jobs(asset_id=asset_id)
        return jobs[0] if jobs else None

    async def search(
        self, request: CanonicalKnowledgeSearchRequest
    ) -> CanonicalKnowledgeSearchResponse:
        started_at = time.perf_counter()
        assets = await self.repository.list_assets(
            tenant_id=request.tenant_id,
            asset_ids=request.asset_ids or None,
            group_id=request.group_id,
        )
        if request.stable_asset_keys:
            keys = set(request.stable_asset_keys)
            assets = [asset for asset in assets if asset.stable_key in keys]
        visible = [asset for asset in assets if _asset_visible(asset, request.user)]
        chunks = await self.repository.get_chunks(
            tenant_id=request.tenant_id,
            asset_ids=[asset.asset_id for asset in visible],
        )
        asset_by_id = {asset.asset_id: asset for asset in visible}
        scored = []
        for chunk in chunks:
            asset = asset_by_id.get(chunk.asset_id)
            if asset is None or chunk.status != "active" or chunk.sensitivity == "secret":
                continue
            score = _score(request.query, chunk.content)
            if score >= 0.2:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].asset_id, item[1].ordinal))
        evidence = []
        used_chars = 0
        warnings = []
        for score, chunk in scored[: request.top_k]:
            remaining = request.max_content_chars - used_chars
            if remaining <= 0:
                warnings.append({"code": "result_truncated"})
                break
            content = chunk.content[:remaining]
            if len(content) < len(chunk.content):
                warnings.append({"code": "result_truncated", "chunk_id": chunk.chunk_id})
            used_chars += len(content)
            evidence.append(
                KnowledgeEvidence(
                    asset_id=chunk.asset_id,
                    chunk_id=chunk.chunk_id,
                    content=content,
                    score=score,
                    source_ref=chunk.source_ref,
                    citation=chunk.citation,
                )
            )
        response = CanonicalKnowledgeSearchResponse(
            matched=bool(evidence),
            evidence=evidence,
            warnings=(
                warnings
                if evidence
                else [
                    {"code": ("permission_filtered" if len(visible) < len(assets) else "no_match")}
                ]
            ),
        )
        await self.repository.save_trace(
            KnowledgeOperationTrace(
                trace_id=response.trace_id,
                tenant_id=request.tenant_id,
                user_id=request.user.id,
                operation="search",
                caller=request.caller,
                purpose=request.purpose,
                policy_outcome="allowed_with_hits" if evidence else "allowed_empty",
                evidence_ids=[item.chunk_id for item in evidence],
                warnings=response.warnings,
                latency_ms=_latency_ms(started_at),
                metadata={
                    "query_sha256": hashlib.sha256(request.query.encode()).hexdigest(),
                    "query_length": len(request.query),
                    "selected_asset_count": len(visible),
                    "filtered_asset_count": len(assets) - len(visible),
                },
            )
        )
        return response

    async def grouped_search(
        self, request: GroupedKnowledgeSearchRequest
    ) -> GroupedKnowledgeSearchResponse:
        started_at = time.perf_counter()
        group = await self.repository.get_group(request.group_id)
        if group is None or group.tenant_id != request.tenant_id:
            raise KnowledgeAssetError("Knowledge Asset Group not found")
        assets = await self.repository.list_assets(
            tenant_id=request.tenant_id, group_id=request.group_id
        )
        by_key = {asset.stable_key: asset for asset in assets if asset.stable_key}
        requested_keys = request.stable_asset_keys or group.stable_asset_keys
        results = []
        for stable_key in requested_keys:
            asset = by_key.get(stable_key)
            if asset is None:
                if request.include_empty_assets:
                    results.append(
                        GroupedKnowledgeAssetResult(
                            stable_key=stable_key,
                            warnings=[{"code": "asset_empty_or_missing"}],
                            trace_id=f"ktrace_{uuid4().hex}",
                        )
                    )
                continue
            response = await self.search(
                CanonicalKnowledgeSearchRequest(
                    query=request.query,
                    user=request.user,
                    caller=request.caller,
                    purpose=request.purpose,
                    tenant_id=request.tenant_id,
                    asset_ids=[asset.asset_id],
                    top_k=request.top_k,
                    max_content_chars=request.max_content_chars,
                )
            )
            results.append(
                GroupedKnowledgeAssetResult(
                    stable_key=stable_key,
                    asset_id=asset.asset_id,
                    matched=response.matched,
                    evidence=response.evidence,
                    warnings=response.warnings,
                    trace_id=response.trace_id,
                )
            )
        response = GroupedKnowledgeSearchResponse(group_id=request.group_id, assets=results)
        await self.repository.save_trace(
            KnowledgeOperationTrace(
                tenant_id=request.tenant_id,
                user_id=request.user.id,
                operation="grouped_search",
                caller=request.caller,
                purpose=request.purpose,
                policy_outcome="allowed",
                evidence_ids=[
                    evidence.chunk_id for result in results for evidence in result.evidence
                ],
                warnings=[warning for result in results for warning in result.warnings],
                latency_ms=_latency_ms(started_at),
                metadata={"group_id": request.group_id, "asset_slot_count": len(results)},
            )
        )
        return response

    async def exact_read(self, request: ExactReadRequest) -> ExactReadResponse:
        started_at = time.perf_counter()
        target = request.target
        asset_ids = (
            [target.asset_id]
            if target.type in {"asset", "source_ref"} and target.asset_id
            else target.asset_ids
        )
        chunk_ids = (
            [target.chunk_id] if target.type == "chunk" and target.chunk_id else target.chunk_ids
        )
        assets = await self.repository.list_assets(
            tenant_id=request.tenant_id, asset_ids=asset_ids or None
        )
        visible_assets = [asset for asset in assets if _asset_visible(asset, request.user)]
        if asset_ids:
            order = {asset_id: index for index, asset_id in enumerate(asset_ids)}
            visible_assets.sort(key=lambda asset: order.get(asset.asset_id, len(order)))
        visible_asset_ids = {asset.asset_id for asset in visible_assets}
        missing = [
            asset_id for asset_id in asset_ids if asset_id not in {a.asset_id for a in assets}
        ]
        filtered = (
            [asset.asset_id for asset in assets if asset.asset_id not in visible_asset_ids]
            if target.type in {"asset", "assets", "source_ref"}
            else []
        )
        chunks = []
        if target.type in {"chunk", "chunks"}:
            found = await self.repository.get_chunks(
                tenant_id=request.tenant_id, chunk_ids=chunk_ids
            )
            chunk_assets = await self.repository.list_assets(
                tenant_id=request.tenant_id,
                asset_ids=list({chunk.asset_id for chunk in found}),
            )
            allowed = {
                asset.asset_id for asset in chunk_assets if _asset_visible(asset, request.user)
            }
            chunks = [
                chunk for chunk in found if chunk.asset_id in allowed and chunk.status == "active"
            ]
            found_ids = {chunk.chunk_id for chunk in found}
            missing.extend([chunk_id for chunk_id in chunk_ids if chunk_id not in found_ids])
            filtered.extend(
                [
                    chunk.chunk_id
                    for chunk in found
                    if chunk.chunk_id not in {c.chunk_id for c in chunks}
                ]
            )
            order = {chunk_id: index for index, chunk_id in enumerate(chunk_ids)}
            chunks.sort(key=lambda chunk: order.get(chunk.chunk_id, len(order)))
        elif target.type in {"asset", "assets"}:
            chunks = await self.repository.get_chunks(
                tenant_id=request.tenant_id,
                asset_ids=[asset.asset_id for asset in visible_assets],
            )
        elif target.type == "source_ref":
            candidates = await self.repository.get_chunks(
                tenant_id=request.tenant_id, asset_ids=list(visible_asset_ids)
            )
            expected = (
                {
                    key: value
                    for key, value in target.source_ref.model_dump(exclude_none=True).items()
                    if value not in ({}, [], "")
                }
                if target.source_ref
                else {}
            )
            chunks = [
                chunk
                for chunk in candidates
                if all(
                    chunk.source_ref.model_dump(exclude_none=True).get(key) == value
                    for key, value in expected.items()
                )
            ]
            if len(chunks) > 1:
                response = ExactReadResponse(
                    assets=visible_assets,
                    warnings=[{"code": "ambiguous_source_ref", "count": len(chunks)}],
                )
                await self._trace_exact(request, response, started_at)
                return response
        chunks = [chunk for chunk in chunks if chunk.sensitivity != "secret"]
        response = ExactReadResponse(
            assets=visible_assets,
            chunks=chunks[request.offset : request.offset + request.limit],
            missing=missing,
            filtered=filtered,
        )
        await self._trace_exact(request, response, started_at)
        return response

    async def _trace_exact(
        self, request: ExactReadRequest, response: ExactReadResponse, started_at: float
    ) -> None:
        await self.repository.save_trace(
            KnowledgeOperationTrace(
                tenant_id=request.tenant_id,
                user_id=request.user.id,
                operation="exact_read",
                caller="host",
                purpose="exact_read",
                policy_outcome="filtered" if response.filtered else "allowed",
                evidence_ids=[chunk.chunk_id for chunk in response.chunks],
                warnings=response.warnings,
                latency_ms=_latency_ms(started_at),
                metadata={
                    "target_type": request.target.type,
                    "asset_count": len(response.assets),
                    "chunk_count": len(response.chunks),
                    "missing_count": len(response.missing),
                    "filtered_count": len(response.filtered),
                },
            )
        )

    async def ingest_bytes(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        file_name: str,
        content_type: str,
        data: bytes,
        name: str | None = None,
        asset_id: str | None = None,
        stable_key: str | None = None,
        group_id: str | None = None,
        replace_asset_id: str | None = None,
        access_policy: KnowledgeAccessPolicy | None = None,
        sensitivity: str = "internal",
        metadata: dict | None = None,
        extracted_chunks: list[tuple[str, KnowledgeSourceRef]] | None = None,
    ) -> tuple[KnowledgeAsset, KnowledgeImportJob]:
        self._validate_upload(file_name, content_type, data)
        now = datetime.now(UTC)
        file_hash = hashlib.sha256(data).hexdigest()
        current = await self.repository.get_asset(replace_asset_id) if replace_asset_id else None
        if current and (current.tenant_id != tenant_id or current.owner_id != owner_id):
            raise KnowledgeAssetError("Replacement Asset ownership conflict")
        asset = KnowledgeAsset(
            asset_id=current.asset_id if current else asset_id or f"kasset_{uuid4().hex}",
            tenant_id=tenant_id,
            owner_id=owner_id,
            stable_key=stable_key or (current.stable_key if current else None),
            group_id=group_id or (current.group_id if current else None),
            name=name or file_name,
            file_name=file_name,
            content_type=content_type,
            size_bytes=len(data),
            file_hash=file_hash,
            status=KnowledgeAssetStatus.UPLOADED,
            access_policy=access_policy or KnowledgeAccessPolicy(),
            sensitivity=sensitivity,
            parser_version="canonical-parser-v1",
            chunking_version="canonical-chunking-v1",
            metadata=metadata or {},
            updated_at=now,
        )
        job = KnowledgeImportJob(
            tenant_id=tenant_id,
            asset_id=asset.asset_id,
            status="pending",
            stage=KnowledgeIngestionStage.UPLOAD,
            created_at=now,
            updated_at=now,
        )
        if current is None:
            await self.repository.save_asset(asset)
        await self.repository.save_job(job)
        try:
            job = await self._advance_job(job, KnowledgeIngestionStage.VALIDATION)
            job = await self._advance_job(job, KnowledgeIngestionStage.PARSING)
            asset = asset.model_copy(
                update={"status": KnowledgeAssetStatus.PROCESSING, "updated_at": datetime.now(UTC)}
            )
            if current is None:
                await self.repository.save_asset(asset)
            parsed = (
                extracted_chunks if extracted_chunks is not None else _parse_text(file_name, data)
            )
            job = await self._advance_job(job, KnowledgeIngestionStage.CHUNKING)
            chunks = [
                KnowledgeAssetChunk(
                    asset_id=asset.asset_id,
                    tenant_id=tenant_id,
                    ordinal=index,
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    source_ref=source_ref,
                    citation={"asset_id": asset.asset_id, "source_ref": source_ref.model_dump()},
                )
                for index, (content, source_ref) in enumerate(parsed)
                if content.strip()
            ]
            job = await self._advance_job(job, KnowledgeIngestionStage.EMBEDDING)
            job = await self._advance_job(job, KnowledgeIngestionStage.INDEXING)
            await self.repository.replace_chunks(asset.asset_id, chunks)
            final_status = KnowledgeAssetStatus.INDEXED if chunks else KnowledgeAssetStatus.DEFERRED
            asset = asset.model_copy(
                update={
                    "status": final_status,
                    "content_hash": hashlib.sha256(
                        "\n".join(chunk.content for chunk in chunks).encode()
                    ).hexdigest(),
                    "updated_at": datetime.now(UTC),
                }
            )
            job = job.model_copy(
                update={
                    "status": "completed",
                    "stage": KnowledgeIngestionStage.COMPLETED,
                    "stage_history": [*job.stage_history, KnowledgeIngestionStage.COMPLETED],
                    "updated_at": datetime.now(UTC),
                    "completed_at": datetime.now(UTC),
                }
            )
        except Exception as exc:
            asset = current or asset.model_copy(
                update={"status": KnowledgeAssetStatus.FAILED, "updated_at": datetime.now(UTC)}
            )
            job = job.model_copy(
                update={
                    "status": "failed",
                    "error": {"code": "ingestion_failed", "message": str(exc)[:500]},
                    "updated_at": datetime.now(UTC),
                    "completed_at": datetime.now(UTC),
                }
            )
        await self.repository.save_asset(asset)
        await self.repository.save_job(job)
        return asset, job

    async def _advance_job(
        self, job: KnowledgeImportJob, stage: KnowledgeIngestionStage
    ) -> KnowledgeImportJob:
        updated = job.model_copy(
            update={
                "status": "running",
                "stage": stage,
                "stage_history": [*job.stage_history, stage],
                "updated_at": datetime.now(UTC),
            }
        )
        return await self.repository.save_job(updated)

    async def retry_job(
        self,
        job_id: str,
        *,
        extracted_chunks: list[tuple[str, KnowledgeSourceRef]],
    ) -> tuple[KnowledgeAsset, KnowledgeImportJob]:
        job = await self.repository.get_job(job_id)
        if job is None or job.status != "failed":
            raise KnowledgeAssetError("Knowledge Import Job is not retryable")
        asset = await self.repository.get_asset(job.asset_id)
        if asset is None:
            raise KnowledgeAssetError("Knowledge Asset not found")
        chunks = [
            KnowledgeAssetChunk(
                asset_id=asset.asset_id,
                tenant_id=asset.tenant_id,
                ordinal=index,
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                source_ref=source_ref,
            )
            for index, (content, source_ref) in enumerate(extracted_chunks)
            if content.strip()
        ]
        await self.repository.replace_chunks(asset.asset_id, chunks)
        now = datetime.now(UTC)
        asset = asset.model_copy(update={"status": KnowledgeAssetStatus.INDEXED, "updated_at": now})
        job = job.model_copy(
            update={
                "status": "completed",
                "stage": KnowledgeIngestionStage.COMPLETED,
                "stage_history": [
                    *job.stage_history,
                    KnowledgeIngestionStage.CHUNKING,
                    KnowledgeIngestionStage.EMBEDDING,
                    KnowledgeIngestionStage.INDEXING,
                    KnowledgeIngestionStage.COMPLETED,
                ],
                "attempt": job.attempt + 1,
                "error": None,
                "updated_at": now,
                "completed_at": now,
            }
        )
        return await self.repository.save_asset(asset), await self.repository.save_job(job)

    async def soft_delete(self, asset_id: str, *, tenant_id: str, owner_id: str) -> KnowledgeAsset:
        asset = await self.repository.get_asset(asset_id)
        if asset is None or asset.tenant_id != tenant_id or asset.owner_id != owner_id:
            raise KnowledgeAssetError("Knowledge Asset not found")
        now = datetime.now(UTC)
        deleted = asset.model_copy(
            update={
                "status": KnowledgeAssetStatus.DELETED,
                "deleted_at": now,
                "updated_at": now,
            }
        )
        chunks = await self.repository.get_chunks(tenant_id=tenant_id, asset_ids=[asset_id])
        await self.repository.replace_chunks(
            asset_id,
            [chunk.model_copy(update={"status": "deleted", "updated_at": now}) for chunk in chunks],
        )
        return await self.repository.save_asset(deleted)

    def _validate_upload(self, file_name: str, content_type: str, data: bytes) -> None:
        if not file_name or Path(file_name).name != file_name or "\x00" in file_name:
            raise KnowledgeAssetError("Invalid file name")
        if Path(file_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise KnowledgeAssetError("Unsupported file extension")
        if content_type not in SUPPORTED_CONTENT_TYPES:
            raise KnowledgeAssetError("Unsupported content type")
        if not data or len(data) > self.max_file_bytes:
            raise KnowledgeAssetError("Invalid file size")


def _asset_visible(asset: KnowledgeAsset, user: UserContext) -> bool:
    return (
        asset.status == KnowledgeAssetStatus.INDEXED
        and asset.sensitivity != "secret"
        and asset.access_policy.allows(user, asset.tenant_id)
    )


def _score(query: str, content: str) -> float:
    query_tokens = set(_tokens(query))
    content_tokens = set(_tokens(content))
    if not query_tokens or not content_tokens:
        return 0
    overlap = len(query_tokens & content_tokens)
    return min(1.0, overlap / len(query_tokens))


def _tokens(value: str) -> list[str]:
    latin = [token.lower() for token in re.findall(r"[A-Za-z0-9_]+", value)]
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", value)
    chinese = [
        run[index : index + 2] for run in chinese_runs for index in range(max(1, len(run) - 1))
    ]
    return latin + chinese


def _parse_text(file_name: str, data: bytes) -> list[tuple[str, KnowledgeSourceRef]]:
    if Path(file_name).suffix.lower() not in {".txt", ".md", ".csv"}:
        raise KnowledgeAssetError("Parser is not configured for this file type")
    content = data.decode("utf-8")
    return [(content, KnowledgeSourceRef(file_name=file_name, paragraph="document"))]


def _latency_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))
