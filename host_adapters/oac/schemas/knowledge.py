from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import JsonDict, StrictBaseModel


class LegacyKnowledgeScope(StrictBaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    asset_group: str | None = None
    asset_keys: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class LegacyReturnOptions(StrictBaseModel):
    include_content: bool = True
    include_structured_payload: bool = False
    include_asset_metadata: bool = False
    max_content_chars: int = Field(default=12000, ge=0, le=100000)


class LegacyKnowledgeSearchRequest(StrictBaseModel):
    request_id: str
    session_id: str
    user_id: str
    user_tags: list[str] = Field(default_factory=list)
    consumer: str
    consumer_id: str | None = None
    purpose: str
    query: str
    filters: JsonDict = Field(default_factory=dict)
    scope: LegacyKnowledgeScope | None = None
    return_options: LegacyReturnOptions = Field(default_factory=LegacyReturnOptions)
    top_k: int = Field(default=5, ge=0, le=50)


class LegacySourceRef(StrictBaseModel):
    source_type: str = "unknown"
    database: str | None = None
    table: str | None = None
    fields: list[str] = Field(default_factory=list)
    business_id: str | None = None
    sheet: str | None = None
    row: int | None = None
    columns: list[str] = Field(default_factory=list)
    document_name: str | None = None
    section_path: list[str] = Field(default_factory=list)
    page: int | None = None
    paragraph: str | None = None
    uri: str | None = None


class LegacyEvidence(StrictBaseModel):
    evidence_id: str
    chunk_id: str
    asset_id: str
    content: str = ""
    title: str | None = None
    source_type: str
    source_name: str
    source_ref: LegacySourceRef
    business_domain: str = ""
    confidence: float = Field(ge=0, le=1)
    freshness: str = "current"
    sensitivity: str = "internal"
    updated_at: datetime | None = None
    structured_payload: JsonDict | None = None
    asset_metadata: JsonDict | None = None


class LegacyKnowledgeSearchResponse(StrictBaseModel):
    request_id: str
    matched: bool
    confidence: float = Field(ge=0, le=1)
    evidence: list[LegacyEvidence] = Field(default_factory=list)
    warnings: list[JsonDict] = Field(default_factory=list)
    trace_id: str


class LegacyGroupedSearchRequest(StrictBaseModel):
    request_id: str
    session_id: str
    user_id: str
    user_tags: list[str] = Field(default_factory=list)
    consumer: str
    consumer_id: str | None = None
    purpose: str
    query: str
    asset_group: str
    asset_keys: list[str] = Field(default_factory=list)
    top_k_per_asset: int = Field(default=5, ge=0, le=50)
    include_empty_assets: bool = False
    return_options: LegacyReturnOptions = Field(default_factory=LegacyReturnOptions)


class LegacyGroupedAssetResult(StrictBaseModel):
    asset_group: str
    asset_key: str
    asset_name: str
    asset_id: str | None = None
    status: str | None = None
    source_file: str | None = None
    business_domain: str = ""
    matched: bool
    confidence: float = Field(ge=0, le=1)
    evidence: list[LegacyEvidence] = Field(default_factory=list)
    warnings: list[JsonDict] = Field(default_factory=list)
    trace_id: str


class LegacyGroupedSearchResponse(StrictBaseModel):
    request_id: str
    query: str
    asset_group: str
    matched: bool
    confidence: float = Field(ge=0, le=1)
    assets: dict[str, LegacyGroupedAssetResult]
    warnings: list[JsonDict] = Field(default_factory=list)
    trace_ids: dict[str, str] = Field(default_factory=dict)


class LegacyExactReadTarget(StrictBaseModel):
    type: Literal["asset", "assets", "chunk", "chunks", "source_ref"]
    asset_id: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    chunk_id: str | None = None
    chunk_ids: list[str] = Field(default_factory=list)
    source_ref: LegacySourceRef | None = None


class LegacyKnowledgeReadRequest(StrictBaseModel):
    request_id: str
    session_id: str
    user_id: str
    user_tags: list[str] = Field(default_factory=list)
    consumer: str
    consumer_id: str | None = None
    purpose: str
    target: LegacyExactReadTarget
    return_options: LegacyReturnOptions = Field(default_factory=LegacyReturnOptions)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=1000)


class LegacyKnowledgeChunk(StrictBaseModel):
    chunk_id: str
    asset_id: str
    title: str | None = None
    content: str = ""
    content_snippet: str | None = None
    structured_payload: JsonDict | None = None
    source_ref: LegacySourceRef
    business_domain: str = ""
    sensitivity: str = "internal"
    allowed_user_tags: list[str] = Field(default_factory=list)
    freshness: str = "current"
    enabled: bool = True
    content_hash: str
    indexed: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LegacyKnowledgeAsset(StrictBaseModel):
    asset_id: str
    source_type: str
    source_name: str
    business_domain: str = ""
    owner: str = ""
    sensitivity: str = "internal"
    allowed_user_tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    freshness: str = "current"
    searchable: bool | None = None
    status: str | None = None
    original_filename: str | None = None
    file_size: int | None = None
    checksum: str | None = None
    chunk_count: int = 0
    visible_chunk_count: int | None = None
    warnings_count: int | None = None
    metadata: JsonDict | None = None
    chunks: list[LegacyKnowledgeChunk] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LegacyPagination(StrictBaseModel):
    offset: int
    limit: int
    total: int
    returned: int
    has_more: bool


class LegacyKnowledgeReadResponse(StrictBaseModel):
    request_id: str
    target_type: str
    assets: list[LegacyKnowledgeAsset] = Field(default_factory=list)
    chunks: list[LegacyKnowledgeChunk] = Field(default_factory=list)
    pagination: LegacyPagination
    warnings: list[JsonDict] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


class LegacyAssetListResponse(StrictBaseModel):
    request_id: str
    items: list[LegacyKnowledgeAsset]
    total: int
    warnings: list[JsonDict] = Field(default_factory=list)


class LegacyAssetDetailResponse(StrictBaseModel):
    request_id: str
    asset: LegacyKnowledgeAsset
    warnings: list[JsonDict] = Field(default_factory=list)


class LegacyAdminJob(StrictBaseModel):
    job_id: str
    asset_id: str
    status: str
    stage: str
    file_metadata: JsonDict | None = None
    warnings: list[JsonDict] = Field(default_factory=list)
    error_message: str | None = None
    total_chunks: int = 0
    parsed_chunks: int = 0
    indexed_chunks: int = 0
    retry_count: int = 0
    created_by: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LegacyAdminMutationResponse(StrictBaseModel):
    asset: LegacyKnowledgeAsset
    job: LegacyAdminJob


class LegacyAdminListResponse(StrictBaseModel):
    items: list[LegacyKnowledgeAsset]
    total: int


class LegacyAdminDetailResponse(StrictBaseModel):
    asset: LegacyKnowledgeAsset
    metadata: JsonDict = Field(default_factory=dict)
    file_metadata: JsonDict | None = None
    latest_job: LegacyAdminJob | None = None
    chunk_count: int
    indexed_chunk_count: int
    warnings: list[JsonDict] = Field(default_factory=list)


class LegacyAdminDeleteRequest(StrictBaseModel):
    reason: str = Field(min_length=1, max_length=500)


class LegacyAdminDeleteResponse(StrictBaseModel):
    asset_id: str
    deleted: bool
    cleanup_warnings: list[JsonDict] = Field(default_factory=list)
    job: LegacyAdminJob


class LegacyAdminChunksResponse(StrictBaseModel):
    items: list[LegacyKnowledgeChunk]
    total: int
    offset: int
    limit: int
