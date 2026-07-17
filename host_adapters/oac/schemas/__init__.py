"""IRS-compatible OAC wire schemas.

These schemas are transport contracts and must not become OIR core schemas.
"""

from host_adapters.oac.schemas.capabilities import (
    CapabilityModes,
    CapabilityVersions,
    DependencyHealth,
    GovernanceStatus,
    HostCapabilityResponse,
)

__all__ = [
    "CapabilityModes",
    "CapabilityVersions",
    "DependencyHealth",
    "GovernanceStatus",
    "HostCapabilityResponse",
]
