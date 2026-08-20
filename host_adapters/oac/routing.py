"""OAC legacy Registry compatibility for the host-neutral Snapshot routing port."""

from app.application import (
    RegistryApplicationPort,
    RoutingApplicationPort,
    SnapshotRoutingApplicationPort,
)
from app.schemas.common import UserContext
from app.schemas.plans import Plan
from app.schemas.routing import RouteRequest, RouteResponse
from host_adapters.oac.mappers.registry import registry_definition_to_native_v2


class OacLegacyRegistryRoutingAdapter(RoutingApplicationPort):
    """Translate frozen OAC Registry data before Core selects a v2 Handling.

    The legacy Registry remains the OAC wire/storage contract during the staged
    migration. Its bot and route fields cross the adapter boundary exactly once,
    then Core routes only the resulting v2 Definitions and trusted Bindings.
    """

    def __init__(
        self,
        *,
        registry: RegistryApplicationPort,
        snapshot_routing: SnapshotRoutingApplicationPort,
    ) -> None:
        self._registry = registry
        self._snapshot_routing = snapshot_routing

    async def route(self, request: RouteRequest) -> RouteResponse:
        definitions = await self._registry.available_definitions(request.user)
        canonical_definitions = [
            registry_definition_to_native_v2(definition) for definition in definitions
        ]
        return await self._snapshot_routing.route_with_snapshot(
            request,
            definitions=canonical_definitions,
            source="oac_legacy_registry",
        )

    async def preflight_plan(self, plan: Plan, *, user: UserContext) -> None:
        if plan.status in {"completed", "failed", "cancelled"}:
            return
        definitions = await self._registry.available_definitions(user)
        canonical_definitions = [
            registry_definition_to_native_v2(definition) for definition in definitions
        ]
        await self._snapshot_routing.preflight_plan_with_snapshot(
            plan,
            user=user,
            definitions=canonical_definitions,
            source="oac_legacy_registry",
        )
