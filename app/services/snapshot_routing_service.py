"""Route requests through a fresh immutable v2 Registry Snapshot."""

from collections.abc import Callable, Sequence

from app.application import (
    ExternalExecutorApplicationPort,
    RoutingApplicationPort,
    SnapshotRoutingApplicationPort,
)
from app.core.errors import InvocationBindingUnavailableError, PlanBindingUnavailableError
from app.runtime.catalog import RuntimeCatalog
from app.schemas.agents import AgentDefinitionV2, InvocationHandling
from app.schemas.common import UserContext
from app.schemas.plans import Plan
from app.schemas.routing import RouteRequest, RouteResponse
from app.services.binding_resolution import BindingResolver
from app.services.plan_bindings import revalidate_plan_step_binding_against_snapshot
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
        snapshot_runtime = self._build_snapshot_runtime(definitions, source=source)
        return await self._router_factory(snapshot_runtime).route(request)

    async def preflight_plan_with_snapshot(
        self,
        plan: Plan,
        *,
        user: UserContext,
        definitions: Sequence[AgentDefinitionV2],
        source: str,
    ) -> None:
        """Check a delayed Plan without opening a Turn or changing its state."""

        if plan.status in {"completed", "failed", "cancelled"}:
            return
        snapshot_runtime = self._build_snapshot_runtime(definitions, source=source)
        snapshot = snapshot_runtime.snapshot
        if snapshot is None:  # pragma: no cover - load always commits or raises
            raise PlanBindingUnavailableError(
                "Plan Binding is unavailable",
                details={"reason_code": "plan_snapshot_unavailable"},
            )
        resolver = (
            BindingResolver(self._runtime_catalog) if self._runtime_catalog is not None else None
        )
        for step in plan.steps:
            if step.status in {"completed", "failed", "cancelled"}:
                continue
            selection = revalidate_plan_step_binding_against_snapshot(
                step,
                snapshot=snapshot,
                user=user,
            )
            if (
                step.agent_revision is None
                or step.binding_requirement is None
                or not isinstance(selection.definition.handling, InvocationHandling)
            ):
                continue
            if resolver is None:
                raise PlanBindingUnavailableError(
                    "Plan Binding is unavailable",
                    details={"reason_code": "binding_resolver_unavailable"},
                )
            try:
                resolver.resolve_direct_invocation(selection)
            except InvocationBindingUnavailableError as exc:
                raise PlanBindingUnavailableError(
                    "Plan Binding is unavailable",
                    details=exc.details,
                ) from exc

    def _build_snapshot_runtime(
        self,
        definitions: Sequence[AgentDefinitionV2],
        *,
        source: str,
    ) -> RegistrySnapshotRuntime:
        snapshot_runtime = RegistrySnapshotRuntime(
            RegistrySnapshotBuilder(
                self._runtime_catalog,
                external_executor=self._external_executor,
            )
        )
        snapshot_runtime.load(definitions, source=source)
        return snapshot_runtime
