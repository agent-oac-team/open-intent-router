from app.schemas.common import UserContext
from app.schemas.knowledge_assets import (
    CanonicalKnowledgeSearchRequest,
    CanonicalKnowledgeSearchResponse,
    ExactReadRequest,
    ExactReadResponse,
    ExactReadTarget,
    GroupedKnowledgeSearchRequest,
    GroupedKnowledgeSearchResponse,
    KnowledgeAsset,
    KnowledgeAssetChunk,
    KnowledgeEvidence,
    KnowledgeImportJob,
    KnowledgeSourceRef,
)
from host_adapters.oac.schemas.knowledge import (
    LegacyAdminJob,
    LegacyEvidence,
    LegacyGroupedAssetResult,
    LegacyGroupedSearchRequest,
    LegacyGroupedSearchResponse,
    LegacyKnowledgeAsset,
    LegacyKnowledgeChunk,
    LegacyKnowledgeReadRequest,
    LegacyKnowledgeReadResponse,
    LegacyKnowledgeSearchRequest,
    LegacyKnowledgeSearchResponse,
    LegacyPagination,
    LegacySourceRef,
)


def search_request_to_native(
    request: LegacyKnowledgeSearchRequest, *, user: UserContext, tenant_id: str
) -> CanonicalKnowledgeSearchRequest:
    scope = request.scope
    filters = request.filters
    asset_ids = scope.asset_ids if scope else _string_list(filters.get("asset_ids"))
    group_id = scope.asset_group if scope else _string_value(filters.get("asset_group"))
    asset_keys = scope.asset_keys if scope else _string_list(filters.get("asset_keys"))
    return CanonicalKnowledgeSearchRequest(
        query=request.query,
        user=user,
        caller=request.consumer,
        purpose=request.purpose,
        tenant_id=tenant_id,
        asset_ids=asset_ids,
        group_id=group_id,
        stable_asset_keys=asset_keys,
        top_k=request.top_k,
        max_content_chars=request.return_options.max_content_chars,
    )


def search_response_to_compat(
    request: LegacyKnowledgeSearchRequest,
    response: CanonicalKnowledgeSearchResponse,
) -> LegacyKnowledgeSearchResponse:
    evidence = [
        _evidence(item, request.return_options.include_structured_payload)
        for item in response.evidence
    ]
    return LegacyKnowledgeSearchResponse(
        request_id=request.request_id,
        matched=response.matched,
        confidence=max((item.confidence for item in evidence), default=0),
        evidence=evidence,
        warnings=response.warnings,
        trace_id=response.trace_id,
    )


def grouped_request_to_native(
    request: LegacyGroupedSearchRequest, *, user: UserContext, tenant_id: str
) -> GroupedKnowledgeSearchRequest:
    return GroupedKnowledgeSearchRequest(
        query=request.query,
        user=user,
        caller=request.consumer,
        purpose=request.purpose,
        tenant_id=tenant_id,
        group_id=request.asset_group,
        stable_asset_keys=request.asset_keys,
        top_k=request.top_k_per_asset,
        max_content_chars=request.return_options.max_content_chars,
        include_empty_assets=request.include_empty_assets,
    )


def grouped_response_to_compat(
    request: LegacyGroupedSearchRequest,
    response: GroupedKnowledgeSearchResponse,
    *,
    assets: dict[str, KnowledgeAsset],
) -> LegacyGroupedSearchResponse:
    projected = {}
    trace_ids = {}
    for item in response.assets:
        asset = assets.get(item.asset_id or "")
        evidence = [
            _evidence(value, request.return_options.include_structured_payload)
            for value in item.evidence
        ]
        projected[item.stable_key] = LegacyGroupedAssetResult(
            asset_group=request.asset_group,
            asset_key=item.stable_key,
            asset_name=asset.name if asset else item.stable_key,
            asset_id=item.asset_id,
            status=asset.status.value if asset else None,
            source_file=asset.file_name if asset else None,
            business_domain=_asset_domain(asset),
            matched=item.matched,
            confidence=max((value.confidence for value in evidence), default=0),
            evidence=evidence,
            warnings=item.warnings,
            trace_id=item.trace_id,
        )
        trace_ids[item.stable_key] = item.trace_id
    confidence = max((item.confidence for item in projected.values()), default=0)
    return LegacyGroupedSearchResponse(
        request_id=request.request_id,
        query=request.query,
        asset_group=request.asset_group,
        matched=any(item.matched for item in projected.values()),
        confidence=confidence,
        assets=projected,
        warnings=[warning for item in projected.values() for warning in item.warnings],
        trace_ids=trace_ids,
    )


def read_request_to_native(
    request: LegacyKnowledgeReadRequest, *, user: UserContext, tenant_id: str
) -> ExactReadRequest:
    target = request.target
    return ExactReadRequest(
        tenant_id=tenant_id,
        user=user,
        target=ExactReadTarget(
            type=target.type,
            asset_id=target.asset_id,
            asset_ids=target.asset_ids,
            chunk_id=target.chunk_id,
            chunk_ids=target.chunk_ids,
            source_ref=_source_ref_to_native(target.source_ref) if target.source_ref else None,
        ),
        offset=request.offset,
        limit=request.limit,
    )


def read_response_to_compat(
    request_id: str,
    target_type: str,
    response: ExactReadResponse,
    *,
    consumer: str,
    consumer_id: str | None,
    purpose: str,
    offset: int,
    limit: int,
    include_structured_payload: bool = False,
) -> LegacyKnowledgeReadResponse:
    chunks = [
        _chunk(chunk, include_structured_payload=include_structured_payload)
        for chunk in response.chunks
    ]
    by_asset: dict[str, list[LegacyKnowledgeChunk]] = {}
    for chunk in chunks:
        by_asset.setdefault(chunk.asset_id, []).append(chunk)
    assets = [
        _asset(
            asset,
            chunks=by_asset.get(asset.asset_id, []),
            visible_count=len(by_asset.get(asset.asset_id, [])),
        )
        for asset in response.assets
    ]
    total = len(chunks)
    warnings = list(response.warnings)
    warnings.extend({"code": "missing", "id": value} for value in response.missing)
    warnings.extend({"code": "filtered", "id": value} for value in response.filtered)
    return LegacyKnowledgeReadResponse(
        request_id=request_id,
        target_type=target_type,
        assets=assets,
        chunks=chunks,
        pagination=LegacyPagination(
            offset=offset,
            limit=limit,
            total=total,
            returned=len(chunks),
            has_more=offset + len(chunks) < total,
        ),
        warnings=warnings,
        metadata={
            "missing_count": len(response.missing),
            "filtered_count": len(response.filtered),
            "consumer": consumer,
            "consumer_id": consumer_id,
            "purpose": purpose,
        },
    )


def admin_asset(asset: KnowledgeAsset, *, chunk_count: int) -> LegacyKnowledgeAsset:
    return _asset(asset, chunks=[], visible_count=chunk_count, admin=True)


def admin_job(
    job: KnowledgeImportJob, asset: KnowledgeAsset, *, chunk_count: int
) -> LegacyAdminJob:
    status = (
        "indexed"
        if job.status == "completed"
        else "failed"
        if job.status == "failed"
        else job.status
    )
    stage = "indexed" if job.status == "completed" else job.stage.value
    return LegacyAdminJob(
        job_id=job.job_id,
        asset_id=job.asset_id,
        status=status,
        stage=stage,
        file_metadata={
            "original_filename": asset.file_name,
            "content_type": asset.content_type,
            "extension": (asset.file_name or "").rsplit(".", 1)[-1],
            "file_size": asset.size_bytes,
            "sha256": asset.file_hash,
            "parser_version": asset.parser_version,
            "chunking_strategy_version": asset.chunking_version,
            "embedding_model": asset.embedding_version,
        },
        warnings=job.warnings,
        error_message=(job.error or {}).get("message") if job.error else None,
        total_chunks=chunk_count,
        parsed_chunks=chunk_count,
        indexed_chunks=chunk_count if asset.status.value == "indexed" else 0,
        retry_count=job.attempt - 1,
        created_by=asset.owner_id,
        started_at=job.created_at,
        finished_at=job.completed_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _evidence(item: KnowledgeEvidence, include_structured: bool) -> LegacyEvidence:
    return LegacyEvidence(
        evidence_id=f"ev_{item.chunk_id}",
        chunk_id=item.chunk_id,
        asset_id=item.asset_id,
        content=item.content,
        title=item.citation.get("title") or item.chunk_id,
        source_type=_source_type(item.source_ref),
        source_name=item.source_ref.file_name or item.asset_id,
        source_ref=_source_ref(item.source_ref),
        business_domain=str(item.citation.get("business_domain") or ""),
        confidence=item.score,
        sensitivity=str(item.citation.get("sensitivity") or "internal"),
        structured_payload=(
            item.citation.get("structured_payload")
            or {"row_id": item.chunk_id, "content": item.content}
            if include_structured
            else None
        ),
    )


def _asset(
    asset: KnowledgeAsset,
    *,
    chunks: list[LegacyKnowledgeChunk],
    visible_count: int,
    admin: bool = False,
) -> LegacyKnowledgeAsset:
    return LegacyKnowledgeAsset(
        asset_id=asset.asset_id,
        source_type=str(asset.metadata.get("source_type") or _file_source_type(asset.file_name)),
        source_name=str(asset.metadata.get("source_name") or asset.name),
        business_domain=_asset_domain(asset),
        owner=asset.owner_id,
        sensitivity=asset.sensitivity,
        allowed_user_tags=asset.access_policy.allow_groups,
        enabled=asset.status.value not in {"disabled", "deleted", "failed"},
        searchable=asset.status.value == "indexed" if admin else None,
        status=asset.status.value if admin else None,
        original_filename=asset.file_name if admin else None,
        file_size=asset.size_bytes if admin else None,
        checksum=asset.file_hash if admin else None,
        chunk_count=visible_count,
        visible_chunk_count=visible_count if not admin else None,
        warnings_count=0 if admin else None,
        metadata=asset.metadata or None,
        chunks=chunks,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _chunk(chunk: KnowledgeAssetChunk, *, include_structured_payload: bool) -> LegacyKnowledgeChunk:
    return LegacyKnowledgeChunk(
        chunk_id=chunk.chunk_id,
        asset_id=chunk.asset_id,
        title=chunk.title or chunk.chunk_id,
        content=chunk.content,
        structured_payload=(
            chunk.metadata.get("structured_payload")
            or {"row_id": chunk.chunk_id, "content": chunk.content}
            if include_structured_payload
            else None
        ),
        source_ref=_source_ref(chunk.source_ref),
        business_domain=str(chunk.metadata.get("business_domain") or ""),
        sensitivity=chunk.sensitivity,
        allowed_user_tags=_string_list(chunk.metadata.get("allowed_user_tags")),
        enabled=chunk.status == "active",
        content_hash=chunk.content_hash,
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )


def _source_ref(source_ref: KnowledgeSourceRef) -> LegacySourceRef:
    return LegacySourceRef(
        source_type=_source_type(source_ref),
        sheet=source_ref.sheet,
        row=source_ref.row_start,
        columns=_string_list(source_ref.metadata.get("columns")),
        document_name=source_ref.file_name,
        page=source_ref.page,
        paragraph=source_ref.paragraph,
        uri=source_ref.source_uri,
    )


def _source_ref_to_native(source_ref: LegacySourceRef) -> KnowledgeSourceRef:
    return KnowledgeSourceRef(
        source_uri=source_ref.uri,
        file_name=source_ref.document_name,
        sheet=source_ref.sheet,
        row_start=source_ref.row,
        page=source_ref.page,
        paragraph=source_ref.paragraph,
        metadata={"columns": source_ref.columns},
    )


def _source_type(source_ref: KnowledgeSourceRef) -> str:
    return str(source_ref.metadata.get("source_type") or _file_source_type(source_ref.file_name))


def _file_source_type(file_name: str | None) -> str:
    extension = (file_name or "").lower().rsplit(".", 1)[-1]
    return "excel" if extension in {"xlsx", "xls"} else "document"


def _asset_domain(asset: KnowledgeAsset | None) -> str:
    return str(asset.metadata.get("business_domain") or "") if asset else ""


def _string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _string_value(value) -> str | None:
    return str(value) if value is not None else None
