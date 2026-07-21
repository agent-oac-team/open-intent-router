"""IRS-compatible OAC wire schemas.

These schemas are transport contracts and must not become OIR core schemas.
"""

from host_adapters.oac.schemas.capabilities import (
    AuthorizationCapability,
    CapabilityModes,
    CapabilityVersions,
    DependencyHealth,
    GovernanceStatus,
    HostCapabilityResponse,
)

__all__ = [
    "AuthorizationCapability",
    "CapabilityModes",
    "CapabilityVersions",
    "DependencyHealth",
    "GovernanceStatus",
    "HostCapabilityResponse",
]
