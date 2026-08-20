from collections.abc import Awaitable, Callable

from fastapi import APIRouter

from host_adapters.oac.schemas import HostCapabilityResponse

CapabilityProvider = Callable[[], Awaitable[HostCapabilityResponse]]


def build_capability_router(provider: CapabilityProvider) -> APIRouter:
    router = APIRouter(tags=["host-capabilities"])

    @router.get("/health")
    async def health() -> dict[str, str]:
        """Host liveness must not read the Registry or probe any dependency."""

        return {"status": "ok"}

    @router.get("/capabilities", response_model=HostCapabilityResponse)
    async def capabilities() -> HostCapabilityResponse:
        return await provider()

    return router
