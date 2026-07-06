from typing import Protocol

from app.schemas.routing import LLMRouteInput, RouteResponse


class LLMClient(Protocol):
    async def route(self, payload: LLMRouteInput) -> RouteResponse: ...
