from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CapabilityVersions(BaseModel):
    adapter: str
    core: str
    schema_: str = Field(alias="schema", serialization_alias="schema")
    policy: str

    model_config = ConfigDict(populate_by_name=True)


class CapabilityModes(BaseModel):
    memory: str
    shadow: str


class DependencyHealth(BaseModel):
    core: Literal["ok", "degraded"]
    registry: str


class GovernanceStatus(BaseModel):
    write_fence: Literal["enabled", "disabled"]
    write_freeze: bool


class AuthorizationCapability(BaseModel):
    current_signature_version: Literal["v2"]
    accepted_signature_versions: list[str]
    v1_compatibility_enabled: bool
    central_route_required_signature_version: str
    claims_version: str
    policy_version: str
    bundle_catalog: Literal["ok", "degraded"]
    credential_profile_catalog: Literal["ok", "degraded"]
    signature_usage: list[dict[str, str | int]]


class HostCapabilityResponse(BaseModel):
    status: Literal["ok", "degraded"]
    host: Literal["oac"] = "oac"
    versions: CapabilityVersions
    modes: CapabilityModes
    dependencies: DependencyHealth
    governance: GovernanceStatus
    authorization: AuthorizationCapability
