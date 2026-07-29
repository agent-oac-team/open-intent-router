from fastapi import APIRouter

from host_adapters.oac.api.central import router as central_router
from host_adapters.oac.api.knowledge import router as knowledge_router
from host_adapters.oac.api.memory_governance import router as memory_governance_router
from host_adapters.oac.api.registry import router as registry_router
from host_adapters.oac.api.runtime_observation import router as runtime_observation_router
from host_adapters.oac.api.user_memories import router as user_memories_router

router = APIRouter()
router.include_router(central_router)
router.include_router(knowledge_router)
router.include_router(memory_governance_router)
router.include_router(registry_router)
router.include_router(runtime_observation_router)
router.include_router(user_memories_router)
