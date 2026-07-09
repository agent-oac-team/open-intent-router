from fastapi import APIRouter, Depends

from app.dependencies import get_knowledge_service
from app.schemas.knowledge import (
    KnowledgeDebugResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    service: KnowledgeService = Depends(get_knowledge_service),
) -> KnowledgeSearchResponse:
    return await service.search(payload)


@router.get("/debug", response_model=KnowledgeDebugResponse)
async def knowledge_debug(
    source_ids: str | None = None,
    caller_type: str | None = None,
    caller_id: str | None = None,
    purpose: str | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    service: KnowledgeService = Depends(get_knowledge_service),
) -> KnowledgeDebugResponse:
    parsed_source_ids = (
        [item.strip() for item in source_ids.split(",") if item.strip()] if source_ids else None
    )
    return await service.debug_state(
        source_ids=parsed_source_ids,
        caller_type=caller_type,
        caller_id=caller_id,
        purpose=purpose,
        tenant_id=tenant_id,
        limit=limit,
    )
