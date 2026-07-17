from app.core.config import Settings
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.fallback.gateway import IRSFallbackGateway
from host_adapters.oac.schemas import (
    CapabilityModes,
    CapabilityVersions,
    DependencyHealth,
    GovernanceStatus,
    HostCapabilityResponse,
)
from host_apps.oac.config import OacHostSettings


class OacHostCapabilityProvider:
    def __init__(
        self,
        *,
        core: Settings,
        host: OacHostSettings,
        ports: OacAdapterApplicationPorts,
        fallback_gateway: IRSFallbackGateway,
    ) -> None:
        self.core = core
        self.host = host
        self.ports = ports
        self.fallback_gateway = fallback_gateway

    async def snapshot(self) -> HostCapabilityResponse:
        registry = await self.ports.registry.load()
        status = "ok" if registry.status == "ok" else "degraded"
        return HostCapabilityResponse(
            status=status,
            versions=CapabilityVersions(
                adapter=self.host.adapter_version,
                core="0.1.0",
                schema=self.host.schema_version,
                policy=self.host.policy_version,
            ),
            modes=CapabilityModes(
                knowledge=(
                    self.core.knowledge_vector_backend if self.core.knowledge_enabled else "off"
                ),
                memory=(self.core.memory_formation_mode if self.core.memory_enabled else "off"),
                shadow=self.host.shadow_mode,
                fallback=self.host.fallback_mode,
            ),
            dependencies=DependencyHealth(core="ok", registry=registry.status),
            governance=GovernanceStatus(
                write_fence="enabled" if self.host.write_fence_enabled else "disabled",
                write_freeze=self.host.write_freeze_enabled,
                circuit=self.fallback_gateway.circuit.snapshot.state,
                circuit_failure_count=self.fallback_gateway.circuit.snapshot.failure_count,
                fallback_event_count=len(self.fallback_gateway.metrics.audit),
            ),
        )
