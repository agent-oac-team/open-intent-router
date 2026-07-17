import json
from pathlib import Path

from app.schemas.knowledge_assets import CanonicalKnowledgeSearchResponse
from host_adapters.oac.mappers.knowledge import search_response_to_compat
from host_adapters.oac.schemas.knowledge import (
    LegacyAdminChunksResponse,
    LegacyAdminDeleteRequest,
    LegacyAdminDeleteResponse,
    LegacyAdminDetailResponse,
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

ROOT = Path("tests/contract/oac_irs/irs-baseline/v1/success")


def _fixture(name: str) -> dict:
    return json.loads((ROOT / f"{name}.json").read_text(encoding="utf-8"))


def test_all_frozen_knowledge_query_and_read_fixtures_parse() -> None:
    search = _fixture("knowledge-search")
    grouped = _fixture("knowledge-grouped-search")
    read = _fixture("knowledge-read")
    assets = _fixture("knowledge-assets-list")
    asset = _fixture("knowledge-asset-detail")
    asset_chunks = _fixture("knowledge-asset-chunks")
    chunk = _fixture("knowledge-chunk-detail")

    assert LegacyKnowledgeSearchRequest.model_validate(search["request"]["body"])
    assert LegacyKnowledgeSearchResponse.model_validate(search["response"]["body"])
    assert LegacyGroupedSearchRequest.model_validate(grouped["request"]["body"])
    assert LegacyGroupedSearchResponse.model_validate(grouped["response"]["body"])
    assert LegacyKnowledgeReadRequest.model_validate(read["request"]["body"])
    assert LegacyKnowledgeReadResponse.model_validate(read["response"]["body"])
    assert LegacyAssetListResponse.model_validate(assets["response"]["body"])
    assert LegacyAssetDetailResponse.model_validate(asset["response"]["body"])
    assert LegacyKnowledgeReadResponse.model_validate(asset_chunks["response"]["body"])
    assert LegacyKnowledgeReadResponse.model_validate(chunk["response"]["body"])


def test_all_frozen_knowledge_admin_response_fixtures_parse() -> None:
    upload = _fixture("knowledge-admin-upload")
    listed = _fixture("knowledge-admin-list")
    detail = _fixture("knowledge-admin-detail")
    deleted = _fixture("knowledge-admin-delete")
    chunks = _fixture("knowledge-admin-chunks")
    retry = _fixture("knowledge-admin-retry")

    assert LegacyAdminMutationResponse.model_validate(upload["response"]["body"])
    assert LegacyAdminListResponse.model_validate(listed["response"]["body"])
    assert LegacyAdminDetailResponse.model_validate(detail["response"]["body"])
    assert LegacyAdminDeleteRequest.model_validate(deleted["request"]["body"])
    assert LegacyAdminDeleteResponse.model_validate(deleted["response"]["body"])
    assert LegacyAdminChunksResponse.model_validate(chunks["response"]["body"])
    assert LegacyAdminMutationResponse.model_validate(retry["response"]["body"])


def test_provider_failure_projects_http_200_compatible_warning_shape() -> None:
    request = LegacyKnowledgeSearchRequest.model_validate(
        _fixture("knowledge-search")["request"]["body"]
    )
    response = search_response_to_compat(
        request,
        CanonicalKnowledgeSearchResponse(
            matched=False,
            warnings=[{"code": "embedding_error", "provider": "embedding"}],
        ),
    )

    assert response.matched is False
    assert response.confidence == 0
    assert response.evidence == []
    assert response.warnings == [{"code": "embedding_error", "provider": "embedding"}]
