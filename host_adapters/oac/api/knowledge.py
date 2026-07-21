from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.schemas.common import UserContext
from app.schemas.knowledge_assets import (
    ExactReadRequest,
    ExactReadTarget,
    KnowledgeAccessPolicy,
)
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.fallback.gateway import (
    FallbackBlockedError,
    IRSFallbackGateway,
    IRSLegacyClient,
)
from host_adapters.oac.fallback.policy import classify_operation
from host_adapters.oac.identity import HostOperation, authorize_host_operation
from host_adapters.oac.identity.models import HostAuthorizationError, TrustedHostIdentity
from host_adapters.oac.mappers.knowledge import (
    admin_asset,
    admin_job,
    grouped_request_to_native,
    grouped_response_to_compat,
    read_request_to_native,
    read_response_to_compat,
    search_request_to_native,
    search_response_to_compat,
)
from host_adapters.oac.schemas.knowledge import (
    LegacyAdminChunksResponse,
    LegacyAdminDeleteRequest,
    LegacyAdminDeleteResponse,
    LegacyAdminDetailResponse,
    LegacyAdminJob,
    LegacyAdminListResponse,
    LegacyAdminMutationResponse,
    LegacyAssetDetailResponse,
    LegacyAssetListResponse,
    LegacyGroupedSearchRequest,
    LegacyGroupedSearchResponse,
    LegacyKnowledgeReadRequest,
    LegacyKnowledgeReadResponse,
    LegacyKnowledgeSearchRequest,
    LegacyKnowledgeSearchResponse,
)
from host_apps.oac.dependencies import (
    get_irs_fallback_gateway,
    get_irs_legacy_client,
    get_oac_adapter_application_ports,
    get_trusted_host_identity,
)

router = APIRouter(tags=["legacy-knowledge"])


@router.post("/api/v1/knowledge/search", response_model=LegacyKnowledgeSearchResponse)
async def search_knowledge(
    request: LegacyKnowledgeSearchRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyKnowledgeSearchResponse:
    _authorize(identity, "read_only")

    async def primary() -> LegacyKnowledgeSearchResponse:
        response = await ports.knowledge_assets.search(
            search_request_to_native(request, user=_user(identity), tenant_id=identity.tenant_id)
        )
        return search_response_to_compat(request, response)

    async def fallback() -> LegacyKnowledgeSearchResponse:
        payload = await irs.request_json(
            method="POST",
            path="/api/v1/knowledge/search",
            json_body=request.model_dump(mode="json"),
        )
        return LegacyKnowledgeSearchResponse.model_validate(payload)

    return await _execute_read_fallback(
        fallback_gateway,
        operation_path="/api/v1/knowledge/search",
        request_id=request.request_id,
        primary=primary,
        fallback=fallback,
    )


@router.post("/api/v1/knowledge/grouped-search", response_model=LegacyGroupedSearchResponse)
async def grouped_search_knowledge(
    request: LegacyGroupedSearchRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyGroupedSearchResponse:
    _authorize(identity, "read_only")

    async def primary() -> LegacyGroupedSearchResponse:
        response = await ports.knowledge_assets.grouped_search(
            grouped_request_to_native(request, user=_user(identity), tenant_id=identity.tenant_id)
        )
        assets = {
            asset.asset_id: asset
            for asset in await ports.knowledge_assets.list_assets(tenant_id=identity.tenant_id)
        }
        return grouped_response_to_compat(request, response, assets=assets)

    async def fallback() -> LegacyGroupedSearchResponse:
        payload = await irs.request_json(
            method="POST",
            path="/api/v1/knowledge/grouped-search",
            json_body=request.model_dump(mode="json"),
        )
        return LegacyGroupedSearchResponse.model_validate(payload)

    return await _execute_read_fallback(
        fallback_gateway,
        operation_path="/api/v1/knowledge/grouped-search",
        request_id=request.request_id,
        primary=primary,
        fallback=fallback,
    )


@router.post("/api/v1/knowledge/read", response_model=LegacyKnowledgeReadResponse)
async def read_knowledge(
    request: LegacyKnowledgeReadRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyKnowledgeReadResponse:
    _authorize(identity, "read_only")

    async def primary() -> LegacyKnowledgeReadResponse:
        response = await ports.knowledge_assets.exact_read(
            read_request_to_native(request, user=_user(identity), tenant_id=identity.tenant_id)
        )
        return read_response_to_compat(
            request.request_id,
            request.target.type,
            response,
            consumer=request.consumer,
            consumer_id=request.consumer_id,
            purpose=request.purpose,
            offset=request.offset,
            limit=request.limit,
            include_structured_payload=request.return_options.include_structured_payload,
        )

    async def fallback() -> LegacyKnowledgeReadResponse:
        payload = await irs.request_json(
            method="POST",
            path="/api/v1/knowledge/read",
            json_body=request.model_dump(mode="json"),
        )
        return LegacyKnowledgeReadResponse.model_validate(payload)

    return await _execute_read_fallback(
        fallback_gateway,
        operation_path="/api/v1/knowledge/read",
        request_id=request.request_id,
        primary=primary,
        fallback=fallback,
    )


@router.get("/api/v1/knowledge/assets", response_model=LegacyAssetListResponse)
async def list_knowledge_assets(
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyAssetListResponse:
    _authorize(identity, "read_only")
    request_id = f"req_k_access_{uuid4().hex}"

    async def primary() -> LegacyAssetListResponse:
        assets = await ports.knowledge_assets.list_assets(tenant_id=identity.tenant_id)
        visible = []
        user = _user(identity)
        for asset in assets:
            if asset.deleted_at is not None or not asset.access_policy.allows(
                user, identity.tenant_id
            ):
                continue
            result = await ports.knowledge_assets.exact_read(
                ExactReadRequest(
                    tenant_id=identity.tenant_id,
                    user=_user(identity),
                    target=ExactReadTarget(type="asset", asset_id=asset.asset_id),
                    limit=1,
                )
            )
            if result.assets:
                visible.append(admin_asset(result.assets[0], chunk_count=len(result.chunks)))
            else:
                visible.append(admin_asset(asset, chunk_count=0))
        return LegacyAssetListResponse(request_id=request_id, items=visible, total=len(visible))

    async def fallback() -> LegacyAssetListResponse:
        return LegacyAssetListResponse.model_validate(
            await irs.request_json(method="GET", path="/api/v1/knowledge/assets")
        )

    return await _execute_read_fallback(
        fallback_gateway,
        method="GET",
        operation_path="/api/v1/knowledge/assets",
        request_id=request_id,
        primary=primary,
        fallback=fallback,
    )


@router.get("/api/v1/knowledge/assets/{asset_id}", response_model=LegacyAssetDetailResponse)
async def get_knowledge_asset(
    asset_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyAssetDetailResponse:
    request_id = f"req_k_access_{uuid4().hex}"

    async def primary() -> LegacyAssetDetailResponse:
        result = await _read_asset(identity, ports, asset_id)
        return LegacyAssetDetailResponse(
            request_id=request_id,
            asset=admin_asset(result.assets[0], chunk_count=len(result.chunks)),
        )

    async def fallback() -> LegacyAssetDetailResponse:
        return LegacyAssetDetailResponse.model_validate(
            await irs.request_json(method="GET", path=f"/api/v1/knowledge/assets/{asset_id}")
        )

    return await _execute_read_fallback(
        fallback_gateway,
        method="GET",
        operation_path=f"/api/v1/knowledge/assets/{asset_id}",
        request_id=request_id,
        primary=primary,
        fallback=fallback,
    )


@router.get(
    "/api/v1/knowledge/assets/{asset_id}/chunks",
    response_model=LegacyKnowledgeReadResponse,
)
async def get_knowledge_asset_chunks(
    asset_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=1000),
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyKnowledgeReadResponse:
    request_id = f"req_k_access_{uuid4().hex}"

    async def primary() -> LegacyKnowledgeReadResponse:
        result = await _read_asset(identity, ports, asset_id, offset=offset, limit=limit)
        return read_response_to_compat(
            request_id,
            "asset",
            result,
            consumer="central",
            consumer_id="knowledge_api",
            purpose="answer",
            offset=offset,
            limit=limit,
        )

    async def fallback() -> LegacyKnowledgeReadResponse:
        return LegacyKnowledgeReadResponse.model_validate(
            await irs.request_json(
                method="GET",
                path=f"/api/v1/knowledge/assets/{asset_id}/chunks",
                query={"offset": offset, "limit": limit},
            )
        )

    return await _execute_read_fallback(
        fallback_gateway,
        method="GET",
        operation_path=f"/api/v1/knowledge/assets/{asset_id}/chunks",
        request_id=request_id,
        primary=primary,
        fallback=fallback,
    )


@router.get("/api/v1/knowledge/chunks/{chunk_id}", response_model=LegacyKnowledgeReadResponse)
async def get_knowledge_chunk(
    chunk_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyKnowledgeReadResponse:
    _authorize(identity, "read_only")
    request_id = f"req_k_access_{uuid4().hex}"

    async def primary() -> LegacyKnowledgeReadResponse:
        result = await ports.knowledge_assets.exact_read(
            ExactReadRequest(
                tenant_id=identity.tenant_id,
                user=_user(identity),
                target=ExactReadTarget(type="chunk", chunk_id=chunk_id),
            )
        )
        return read_response_to_compat(
            request_id,
            "chunk",
            result,
            consumer="central",
            consumer_id="knowledge_api",
            purpose="answer",
            offset=0,
            limit=50,
        )

    async def fallback() -> LegacyKnowledgeReadResponse:
        return LegacyKnowledgeReadResponse.model_validate(
            await irs.request_json(method="GET", path=f"/api/v1/knowledge/chunks/{chunk_id}")
        )

    return await _execute_read_fallback(
        fallback_gateway,
        method="GET",
        operation_path=f"/api/v1/knowledge/chunks/{chunk_id}",
        request_id=request_id,
        primary=primary,
        fallback=fallback,
    )


@router.post("/api/v1/admin/knowledge/files", response_model=LegacyAdminMutationResponse)
async def upload_knowledge_file(
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    file: UploadFile = File(...),
    source_name: str = Form(...),
    business_domain: str = Form(default=""),
    sensitivity: str = Form(default="internal"),
    allowed_user_tags: str = Form(default=""),
    replace_asset_id: str | None = Form(default=None),
) -> LegacyAdminMutationResponse:
    _authorize(identity, "control_write")
    data = await file.read()
    asset, job = await ports.knowledge_assets.ingest_bytes(
        tenant_id=identity.tenant_id,
        owner_id=identity.user_id,
        file_name=file.filename or "upload.bin",
        content_type=file.content_type or "application/octet-stream",
        data=data,
        name=source_name,
        replace_asset_id=replace_asset_id,
        access_policy=KnowledgeAccessPolicy(allow_groups=_csv(allowed_user_tags)),
        sensitivity=sensitivity,
        metadata={"business_domain": business_domain, "uploaded_by_source": "oac_admin"},
    )
    chunks = await ports.knowledge_assets.get_chunks(
        tenant_id=identity.tenant_id, asset_ids=[asset.asset_id]
    )
    return LegacyAdminMutationResponse(
        asset=admin_asset(asset, chunk_count=len(chunks)),
        job=admin_job(job, asset, chunk_count=len(chunks)),
    )


@router.get("/api/v1/admin/knowledge/files", response_model=LegacyAdminListResponse)
async def list_admin_knowledge_files(
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyAdminListResponse:
    _authorize(identity, "control_write")

    async def primary() -> LegacyAdminListResponse:
        assets = await ports.knowledge_assets.list_assets(tenant_id=identity.tenant_id)
        items = []
        for asset in assets:
            chunks = await ports.knowledge_assets.get_chunks(
                tenant_id=identity.tenant_id, asset_ids=[asset.asset_id]
            )
            items.append(admin_asset(asset, chunk_count=len(chunks)))
        return LegacyAdminListResponse(items=items, total=len(items))

    async def fallback() -> LegacyAdminListResponse:
        return LegacyAdminListResponse.model_validate(
            await irs.request_json(method="GET", path="/api/v1/admin/knowledge/files")
        )

    return await _execute_read_fallback(
        fallback_gateway,
        method="GET",
        operation_path="/api/v1/admin/knowledge/files",
        request_id=f"admin-list-{uuid4().hex}",
        primary=primary,
        fallback=fallback,
    )


@router.get("/api/v1/admin/knowledge/files/{asset_id}", response_model=LegacyAdminDetailResponse)
async def get_admin_knowledge_file(
    asset_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyAdminDetailResponse:
    _authorize(identity, "control_write")

    async def primary() -> LegacyAdminDetailResponse:
        asset = await ports.knowledge_assets.get_asset(asset_id)
        if asset is None or asset.tenant_id != identity.tenant_id:
            raise HTTPException(status_code=404, detail="knowledge_asset_not_found")
        chunks = await ports.knowledge_assets.get_chunks(
            tenant_id=identity.tenant_id, asset_ids=[asset_id]
        )
        job = await ports.knowledge_assets.latest_job(asset_id)
        file_metadata = (
            admin_job(job, asset, chunk_count=len(chunks)).file_metadata if job else None
        )
        return LegacyAdminDetailResponse(
            asset=admin_asset(asset, chunk_count=len(chunks)),
            metadata=asset.metadata,
            file_metadata=file_metadata,
            latest_job=admin_job(job, asset, chunk_count=len(chunks)) if job else None,
            chunk_count=len(chunks),
            indexed_chunk_count=len([chunk for chunk in chunks if chunk.status == "active"]),
        )

    async def fallback() -> LegacyAdminDetailResponse:
        return LegacyAdminDetailResponse.model_validate(
            await irs.request_json(method="GET", path=f"/api/v1/admin/knowledge/files/{asset_id}")
        )

    return await _execute_read_fallback(
        fallback_gateway,
        method="GET",
        operation_path=f"/api/v1/admin/knowledge/files/{asset_id}",
        request_id=f"admin-detail-{uuid4().hex}",
        primary=primary,
        fallback=fallback,
    )


@router.get(
    "/api/v1/admin/knowledge/files/{asset_id}/chunks",
    response_model=LegacyAdminChunksResponse,
)
async def list_admin_knowledge_chunks(
    asset_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=1000),
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
    fallback_gateway: IRSFallbackGateway = Depends(get_irs_fallback_gateway),
    irs: IRSLegacyClient = Depends(get_irs_legacy_client),
) -> LegacyAdminChunksResponse:
    _authorize(identity, "control_write")

    async def primary() -> LegacyAdminChunksResponse:
        result = await ports.knowledge_assets.get_chunks(
            tenant_id=identity.tenant_id, asset_ids=[asset_id]
        )
        projected = read_response_to_compat(
            "admin",
            "asset",
            type(
                "ReadResult",
                (),
                {
                    "assets": [],
                    "chunks": result,
                    "missing": [],
                    "filtered": [],
                    "warnings": [],
                },
            )(),
            consumer="admin",
            consumer_id=identity.user_id,
            purpose="admin",
            offset=offset,
            limit=limit,
        ).chunks
        for item in projected:
            item.content_snippet = item.content[:500]
            item.content = ""
            item.indexed = item.enabled
        return LegacyAdminChunksResponse(
            items=projected[offset : offset + limit],
            total=len(projected),
            offset=offset,
            limit=limit,
        )

    async def fallback() -> LegacyAdminChunksResponse:
        return LegacyAdminChunksResponse.model_validate(
            await irs.request_json(
                method="GET",
                path=f"/api/v1/admin/knowledge/files/{asset_id}/chunks",
                query={"offset": offset, "limit": limit},
            )
        )

    return await _execute_read_fallback(
        fallback_gateway,
        method="GET",
        operation_path=f"/api/v1/admin/knowledge/files/{asset_id}/chunks",
        request_id=f"admin-chunks-{uuid4().hex}",
        primary=primary,
        fallback=fallback,
    )


@router.delete("/api/v1/admin/knowledge/files/{asset_id}", response_model=LegacyAdminDeleteResponse)
async def delete_admin_knowledge_file(
    asset_id: str,
    _request: LegacyAdminDeleteRequest,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> LegacyAdminDeleteResponse:
    _authorize(identity, "control_write")
    chunks = await ports.knowledge_assets.get_chunks(
        tenant_id=identity.tenant_id, asset_ids=[asset_id]
    )
    asset = await ports.knowledge_assets.soft_delete(
        asset_id, tenant_id=identity.tenant_id, owner_id=identity.user_id
    )
    now = datetime.now(UTC)
    job = LegacyAdminJob(
        job_id=f"kjob_{uuid4().hex}",
        asset_id=asset.asset_id,
        status="deleted",
        stage="cleanup",
        total_chunks=len(chunks),
        parsed_chunks=len(chunks),
        indexed_chunks=0,
        created_by=identity.user_id,
        started_at=now,
        finished_at=now,
    )
    return LegacyAdminDeleteResponse(asset_id=asset_id, deleted=True, job=job)


@router.post(
    "/api/v1/admin/knowledge/files/{asset_id}/retry",
    response_model=LegacyAdminMutationResponse,
)
async def retry_admin_knowledge_file(
    asset_id: str,
    identity: TrustedHostIdentity = Depends(get_trusted_host_identity),
    ports: OacAdapterApplicationPorts = Depends(get_oac_adapter_application_ports),
) -> LegacyAdminMutationResponse:
    _authorize(identity, "control_write")
    asset = await ports.knowledge_assets.get_asset(asset_id)
    job = await ports.knowledge_assets.latest_job(asset_id)
    if asset is None or job is None or job.status != "failed":
        raise HTTPException(status_code=409, detail="knowledge_job_not_retryable")
    existing = await ports.knowledge_assets.get_chunks(
        tenant_id=identity.tenant_id, asset_ids=[asset_id]
    )
    if not existing:
        raise HTTPException(status_code=409, detail="knowledge_source_reparse_required")
    asset, job = await ports.knowledge_assets.retry_job(
        job.job_id,
        extracted_chunks=[(chunk.content, chunk.source_ref) for chunk in existing],
    )
    return LegacyAdminMutationResponse(
        asset=admin_asset(asset, chunk_count=len(existing)),
        job=admin_job(job, asset, chunk_count=len(existing)),
    )


async def _read_asset(
    identity: TrustedHostIdentity,
    ports: OacAdapterApplicationPorts,
    asset_id: str,
    *,
    offset: int = 0,
    limit: int = 50,
):
    _authorize(identity, "read_only")
    result = await ports.knowledge_assets.exact_read(
        ExactReadRequest(
            tenant_id=identity.tenant_id,
            user=_user(identity),
            target=ExactReadTarget(type="asset", asset_id=asset_id),
            offset=offset,
            limit=limit,
        )
    )
    if not result.assets:
        raise HTTPException(status_code=404, detail="knowledge_asset_not_found")
    return result


def _user(identity: TrustedHostIdentity) -> UserContext:
    return UserContext(
        id=identity.user_id,
        groups=list(identity.groups),
        attributes={"tenant_id": identity.tenant_id},
    )


def _authorize(identity: TrustedHostIdentity, operation: HostOperation) -> None:
    try:
        authorize_host_operation(identity, operation)
    except HostAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="host_operation_forbidden") from exc


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def _execute_read_fallback(
    gateway: IRSFallbackGateway,
    *,
    method: str = "POST",
    operation_path: str,
    request_id: str,
    primary,
    fallback,
):
    try:
        return await gateway.execute(
            operation=classify_operation(method, operation_path),
            request_id=request_id,
            primary=primary,
            fallback=fallback,
            correlation={"request_id": request_id},
        )
    except FallbackBlockedError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "fallback_blocked", "reason": exc.reason, "retryable": True},
        ) from exc
