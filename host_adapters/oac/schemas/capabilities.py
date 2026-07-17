from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CapabilityVersions(BaseModel):
    adapter: str
    core: str
    schema_: str = Field(alias="schema", serialization_alias="schema")
    policy: str

    model_config = ConfigDict(populate_by_name=True)


class CapabilityModes(BaseModel):
    knowledge: str
    memory: str
    shadow: str
    fallback: str


class DependencyHealth(BaseModel):
    core: Literal["ok", "degraded"]
    registry: str


class GovernanceStatus(BaseModel):
    write_fence: Literal["enabled", "disabled"]
    write_freeze: bool
    circuit: Literal["closed", "open", "half_open"]
    circuit_failure_count: int
    fallback_event_count: int


class HostCapabilityResponse(BaseModel):
    status: Literal["ok", "degraded"]
    host: Literal["oac"] = "oac"
    versions: CapabilityVersions
    modes: CapabilityModes
    dependencies: DependencyHealth
    governance: GovernanceStatus
