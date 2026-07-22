from app.core.config import Settings
from app.core.memory_runtime import MemoryRuntimePolicy
from host_adapters.oac.application import OacAdapterApplicationPorts
from host_adapters.oac.authz import OAC_BUNDLE_CATALOG
from host_adapters.oac.fallback.gateway import IRSFallbackGateway
from host_adapters.oac.identity.observability import HOST_SIGNATURE_METRICS
from host_adapters.oac.identity.profiles import credential_profile_catalog_healthy
from host_adapters.oac.schemas import (
    AuthorizationCapability,
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
        memory_policy: MemoryRuntimePolicy,
    ) -> None:
        self.core = core
        self.host = host
        self.ports = ports
        self.fallback_gateway = fallback_gateway
        self.memory_policy = memory_policy

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
                memory=self.memory_policy.mode,
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
            authorization=AuthorizationCapability(
                current_signature_version="v2",
                accepted_signature_versions=["v2"],
                v1_compatibility_enabled=False,
                central_route_required_signature_version="v2",
                claims_version=self.host.claims_version,
                policy_version=OAC_BUNDLE_CATALOG.policy_version,
                bundle_catalog="ok",
                credential_profile_catalog=(
                    "ok" if credential_profile_catalog_healthy() else "degraded"
                ),
                signature_usage=HOST_SIGNATURE_METRICS.snapshot(),
            ),
        )
