from dataclasses import dataclass, field

from app.schemas.knowledge import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.routing import RouteRequest, RouteResponse


@dataclass
class RecordingRoutingPort:
    response: RouteResponse
    requests: list[RouteRequest] = field(default_factory=list)

    async def route(self, request: RouteRequest) -> RouteResponse:
        self.requests.append(request)
        return self.response


@dataclass
class RecordingKnowledgePort:
    response: KnowledgeSearchResponse
    requests: list[KnowledgeSearchRequest] = field(default_factory=list)

    async def search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
        self.requests.append(request)
        return self.response
