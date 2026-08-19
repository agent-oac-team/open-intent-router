"""Route requests through a fresh immutable v2 Registry Snapshot."""

from collections.abc import Callable, Sequence

from app.application import (
    ExternalExecutorApplicationPort,
    RoutingApplicationPort,
    SnapshotRoutingApplicationPort,
)
from app.runtime.catalog import RuntimeCatalog
from app.schemas.agents import AgentDefinitionV2
from app.schemas.routing import RouteRequest, RouteResponse
from app.services.registry_snapshot import RegistrySnapshotBuilder, RegistrySnapshotRuntime

SnapshotRouterFactory = Callable[[RegistrySnapshotRuntime], RoutingApplicationPort]


class SnapshotRoutingService(SnapshotRoutingApplicationPort):
    """Compile request-source Definitions before delegating to the normal Router port.

    A fresh Snapshot Runtime prevents concurrent Host requests from replacing one
    another's selected Definition set. The Router then owns ordinary Candidate Set
    selection and returns its usual trusted RouteResponse capability.
    """

    def __init__(
        self,
        *,
        router_factory: SnapshotRouterFactory,
        runtime_catalog: RuntimeCatalog | None = None,
        external_executor: ExternalExecutorApplicationPort | None = None,
    ) -> None:
        self._router_factory = router_factory
        self._runtime_catalog = runtime_catalog
        self._external_executor = external_executor

    async def route_with_snapshot(
        self,
        request: RouteRequest,
        *,
        definitions: Sequence[AgentDefinitionV2],
        source: str,
    ) -> RouteResponse:
        snapshot_runtime = RegistrySnapshotRuntime(
            RegistrySnapshotBuilder(
                self._runtime_catalog,
                external_executor=self._external_executor,
            )
        )
        snapshot_runtime.load(definitions, source=source)
        return await self._router_factory(snapshot_runtime).route(request)
