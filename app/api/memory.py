from fastapi import APIRouter, Depends

from app.dependencies import get_memory_service
from app.schemas.memory import (
    MemoryCleanupResult,
    MemoryDebugResponse,
    MemoryRecallRequest,
    MemoryRecallResponse,
    MemoryWriteCandidate,
    MemoryWriteDecision,
)
from app.services.memory_service import MemoryService

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


@router.post("/recall", response_model=MemoryRecallResponse)
async def recall_memory(
    payload: MemoryRecallRequest,
    service: MemoryService = Depends(get_memory_service),
) -> MemoryRecallResponse:
    return await service.recall(payload)


@router.post("/write-candidates", response_model=list[MemoryWriteDecision])
async def write_memory_candidates(
    payload: list[MemoryWriteCandidate],
    user_id: str,
    tenant_id: str | None = None,
    service: MemoryService = Depends(get_memory_service),
) -> list[MemoryWriteDecision]:
    return await service.write_candidates(candidates=payload, user_id=user_id, tenant_id=tenant_id)


@router.post("/cleanup", response_model=MemoryCleanupResult)
async def cleanup_memory(
    service: MemoryService = Depends(get_memory_service),
) -> MemoryCleanupResult:
    return await service.cleanup_expired()


@router.get("/debug", response_model=MemoryDebugResponse)
async def memory_debug(
    user_id: str | None = None,
    tenant_id: str | None = None,
    agent_id: str | None = None,
    scopes: str | None = None,
    limit: int = 50,
    service: MemoryService = Depends(get_memory_service),
) -> MemoryDebugResponse:
    parsed_scopes = [item.strip() for item in scopes.split(",") if item.strip()] if scopes else None
    return await service.debug_state(
        user_id=user_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        scopes=parsed_scopes,
        limit=limit,
    )
