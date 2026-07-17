from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from app.schemas.common import JsonDict, StrictBaseModel, UserContext


class KnowledgeAssetStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    DISABLED = "disabled"
    DELETED = "deleted"
    DEFERRED = "deferred"


class KnowledgeIngestionStage(StrEnum):
    UPLOAD = "upload"
    VALIDATION = "validation"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    CLEANUP = "cleanup"
    COMPLETED = "completed"


class KnowledgeAccessPolicy(StrictBaseModel):
    allow_users: list[str] = Field(default_factory=list)
    allow_groups: list[str] = Field(default_factory=list)
    allow_roles: list[str] = Field(default_factory=list)
    allow_tenants: list[str] = Field(default_factory=list)

    def allows(self, user: UserContext, tenant_id: str) -> bool:
        if user.tenant_id != tenant_id:
            return False
        if self.allow_users and user.id not in self.allow_users:
            return False
        if self.allow_groups and not set(user.groups) & set(self.allow_groups):
            return False
        if self.allow_roles and not set(user.roles) & set(self.allow_roles):
            return False
        if self.allow_tenants and tenant_id not in self.allow_tenants:
            return False
        return True


class KnowledgeSourceRef(StrictBaseModel):
    source_uri: str | None = None
    file_name: str | None = None
    sheet: str | None = None
    row_start: int | None = Field(default=None, ge=1)
    row_end: int | None = Field(default=None, ge=1)
    cell_range: str | None = None
    page: int | None = Field(default=None, ge=1)
    paragraph: str | None = None
    metadata: JsonDict = Field(default_factory=dict)


class KnowledgeAsset(StrictBaseModel):
    asset_id: str = Field(default_factory=lambda: f"kasset_{uuid4().hex}")
    tenant_id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=128)
    stable_key: str | None = Field(default=None, max_length=128)
    group_id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    description: str = ""
    file_name: str | None = Field(default=None, max_length=512)
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(default=0, ge=0)
    file_hash: str | None = Field(default=None, max_length=128)
    content_hash: str | None = Field(default=None, max_length=128)
    status: KnowledgeAssetStatus = KnowledgeAssetStatus.UPLOADED
    sensitivity: Literal["public", "internal", "restricted", "secret"] = "internal"
    access_policy: KnowledgeAccessPolicy = Field(default_factory=KnowledgeAccessPolicy)
    tags: list[str] = Field(default_factory=list)
    parser_version: str | None = None
    chunking_version: str | None = None
    embedding_version: str | None = None
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None


class KnowledgeAssetChunk(StrictBaseModel):
    chunk_id: str = Field(default_factory=lambda: f"kchunk_{uuid4().hex}")
    asset_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    ordinal: int = Field(ge=0)
    content: str = Field(min_length=1)
    content_hash: str = Field(min_length=1, max_length=128)
    title: str | None = None
    source_ref: KnowledgeSourceRef = Field(default_factory=KnowledgeSourceRef)
    citation: JsonDict = Field(default_factory=dict)
    status: Literal["active", "disabled", "deleted", "failed"] = "active"
    sensitivity: Literal["public", "internal", "restricted", "secret"] = "internal"
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeAssetGroup(StrictBaseModel):
    group_id: str = Field(default_factory=lambda: f"kgroup_{uuid4().hex}")
    tenant_id: str
    name: str
    stable_asset_keys: list[str] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)


class KnowledgeImportJob(StrictBaseModel):
    job_id: str = Field(default_factory=lambda: f"kjob_{uuid4().hex}")
    tenant_id: str
    asset_id: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    stage: KnowledgeIngestionStage = KnowledgeIngestionStage.UPLOAD
    stage_history: list[KnowledgeIngestionStage] = Field(
        default_factory=lambda: [KnowledgeIngestionStage.UPLOAD]
    )
    attempt: int = Field(default=1, ge=1)
    warnings: list[JsonDict] = Field(default_factory=list)
    error: JsonDict | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


class KnowledgeMigrationManifest(StrictBaseModel):
    manifest_id: str = Field(default_factory=lambda: f"kmanifest_{uuid4().hex}")
    tenant_id: str
    source_path: str
    file_hash: str
    pipeline_version: str
    asset_id: str
    chunk_ids: list[str] = Field(default_factory=list)
    collection_name: str | None = None
    migration_status: Literal["pending", "parsed", "indexed", "deferred", "failed"]
    validation_status: Literal["pending", "validated", "failed", "deferred"] = "pending"
    source_refs: list[KnowledgeSourceRef] = Field(default_factory=list)
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeEvidence(StrictBaseModel):
    asset_id: str
    chunk_id: str
    content: str
    score: float = Field(ge=0, le=1)
    source_ref: KnowledgeSourceRef
    citation: JsonDict = Field(default_factory=dict)


class CanonicalKnowledgeSearchRequest(StrictBaseModel):
    query: str
    user: UserContext
    caller: str
    purpose: str
    tenant_id: str
    asset_ids: list[str] = Field(default_factory=list)
    group_id: str | None = None
    stable_asset_keys: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=0, le=50)
    max_content_chars: int = Field(default=12000, ge=0, le=100000)


class CanonicalKnowledgeSearchResponse(StrictBaseModel):
    matched: bool = False
    evidence: list[KnowledgeEvidence] = Field(default_factory=list)
    warnings: list[JsonDict] = Field(default_factory=list)
    trace_id: str = Field(default_factory=lambda: f"ktrace_{uuid4().hex}")


class GroupedKnowledgeSearchRequest(CanonicalKnowledgeSearchRequest):
    group_id: str
    include_empty_assets: bool = False


class GroupedKnowledgeAssetResult(StrictBaseModel):
    stable_key: str
    asset_id: str | None = None
    matched: bool = False
    evidence: list[KnowledgeEvidence] = Field(default_factory=list)
    warnings: list[JsonDict] = Field(default_factory=list)
    trace_id: str


class GroupedKnowledgeSearchResponse(StrictBaseModel):
    group_id: str
    assets: list[GroupedKnowledgeAssetResult] = Field(default_factory=list)


class ExactReadTarget(StrictBaseModel):
    type: Literal["asset", "assets", "chunk", "chunks", "source_ref"]
    asset_id: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    chunk_id: str | None = None
    chunk_ids: list[str] = Field(default_factory=list)
    source_ref: KnowledgeSourceRef | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "ExactReadTarget":
        required = {
            "asset": bool(self.asset_id),
            "assets": bool(self.asset_ids),
            "chunk": bool(self.chunk_id),
            "chunks": bool(self.chunk_ids),
            "source_ref": bool(self.asset_id and self.source_ref),
        }
        if not required[self.type]:
            raise ValueError(f"Exact Read target {self.type} is incomplete")
        return self


class ExactReadRequest(StrictBaseModel):
    tenant_id: str
    user: UserContext
    target: ExactReadTarget
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class ExactReadResponse(StrictBaseModel):
    assets: list[KnowledgeAsset] = Field(default_factory=list)
    chunks: list[KnowledgeAssetChunk] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    filtered: list[str] = Field(default_factory=list)
    warnings: list[JsonDict] = Field(default_factory=list)


class KnowledgeOperationTrace(StrictBaseModel):
    trace_id: str = Field(default_factory=lambda: f"ktrace_{uuid4().hex}")
    tenant_id: str
    user_id: str | None = None
    operation: Literal["search", "grouped_search", "exact_read", "ingest", "retry", "delete"]
    caller: str
    purpose: str
    policy_outcome: str
    evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[JsonDict] = Field(default_factory=list)
    latency_ms: int = Field(ge=0)
    metadata: JsonDict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
