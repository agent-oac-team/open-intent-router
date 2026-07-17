from fastapi import APIRouter

from host_adapters.oac.api.central import router as central_router
from host_adapters.oac.api.knowledge import router as knowledge_router
from host_adapters.oac.api.registry import router as registry_router

router = APIRouter()
router.include_router(central_router)
router.include_router(knowledge_router)
router.include_router(registry_router)
