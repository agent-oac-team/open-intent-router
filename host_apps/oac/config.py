from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import Settings, get_settings

HostEnvironment = Literal["local", "test"]
ShadowMode = Literal["off", "decision", "state_rehearsal"]
FallbackMode = Literal["off", "read_only", "safe_route"]


class OacHostSettings(BaseSettings):
    """Configuration owned by the OAC host, separate from generic OIR settings."""

    profile: Literal["oac"] = "oac"
    environment: HostEnvironment = "local"
    tenant_id: Literal["oac"] = "oac"
    legacy_api_prefix: str = "/api/v1"
    native_mount_prefix: str = "/oir"

    adapter_version: str = "0.1.0"
    schema_version: str = "irs-contract-v1"
    policy_version: str = "oac-host-policy-v1"

    shadow_mode: ShadowMode = "off"
    fallback_mode: FallbackMode = "off"
    irs_fallback_base_url: str | None = None
    irs_fallback_service_token: SecretStr | None = Field(default=None)
    circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    circuit_recovery_seconds: float = Field(default=30.0, gt=0, le=3600)
    write_fence_enabled: bool = True
    write_freeze_enabled: bool = False
    oir_control_write_enabled: bool = True
    irs_control_write_enabled: bool = False
    oir_runtime_write_enabled: bool = True
    irs_runtime_write_enabled: bool = False
    state_rehearsal_database_url: str | None = None
    state_rehearsal_knowledge_collection: str = "oir_knowledge_vectors_rehearsal"
    state_rehearsal_memory_collection: str = "oir_memory_vectors_rehearsal"
    cutover_watermark: str | None = None
    cutover_audit_path: str = ".data/oac_cutover_quarantine.jsonl"
    enforce_registry_single_writer: bool = False
    irs_registry_write_enabled: bool = False
    feishu_registry_sync_enabled: bool = False
    registry_file_restore_enabled: bool = False

    identity_current_key_id: str | None = None
    identity_current_key: SecretStr | None = Field(default=None)
    identity_previous_key_id: str | None = None
    identity_previous_key: SecretStr | None = Field(default=None)
    identity_allowed_groups: str = ""
    identity_allowed_attribute_keys: str = ""
    oir_identity_signing_key: SecretStr | None = Field(default=None)
    execution_ticket_secret: SecretStr | None = Field(default=None)
    execution_ticket_ttl_seconds: int = Field(default=900, ge=30, le=86400)
    execution_ticket_lease_seconds: int = Field(default=30, ge=5, le=300)
    oac_admin_key_id: str | None = None
    oac_admin_credential: SecretStr | None = Field(default=None)
    coze_workflow_key_id: str | None = None
    coze_workflow_credential: SecretStr | None = Field(default=None)

    model_config = SettingsConfigDict(
        env_prefix="OAC_HOST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def identity_audience(self) -> str:
        return f"oac-oir-adapter-{self.environment}"

    @property
    def native_api_prefix(self) -> str:
        return f"{self.native_mount_prefix.rstrip('/')}/api/v1"

    @property
    def cutover_watermark_at(self) -> datetime | None:
        return datetime.fromisoformat(self.cutover_watermark) if self.cutover_watermark else None

    @property
    def allowed_groups(self) -> frozenset[str]:
        return _csv_values(self.identity_allowed_groups)

    @property
    def allowed_attribute_keys(self) -> frozenset[str]:
        return _csv_values(self.identity_allowed_attribute_keys)


@dataclass(frozen=True)
class OacHostProfile:
    core: Settings
    host: OacHostSettings


@lru_cache
def get_oac_host_settings() -> OacHostSettings:
    return OacHostSettings()


def build_oac_host_profile(
    *,
    core: Settings | None = None,
    host: OacHostSettings | None = None,
) -> OacHostProfile:
    profile = OacHostProfile(core=core or get_settings(), host=host or get_oac_host_settings())
    validate_registry_single_writer(profile)
    validate_governance_profile(profile)
    return profile


def validate_registry_single_writer(profile: OacHostProfile) -> None:
    if not profile.host.enforce_registry_single_writer:
        return
    if (
        profile.core.registry_backend != "database"
        or profile.core.registry_file_fallback_on_empty
        or profile.host.irs_registry_write_enabled
        or profile.host.feishu_registry_sync_enabled
        or profile.host.registry_file_restore_enabled
    ):
        raise ValueError("OIR database must be the only writable Registry source")


def validate_governance_profile(profile: OacHostProfile) -> None:
    host = profile.host
    if host.write_fence_enabled:
        if host.oir_control_write_enabled and host.irs_control_write_enabled:
            raise ValueError("control writes cannot have dual writable primaries")
        if host.oir_runtime_write_enabled and host.irs_runtime_write_enabled:
            raise ValueError("runtime writes cannot have dual writable primaries")
        if not host.write_freeze_enabled and (
            not host.oir_control_write_enabled or not host.oir_runtime_write_enabled
        ):
            raise ValueError("OIR must be the only writable primary outside an explicit freeze")
    if host.fallback_mode != "off" and not host.irs_fallback_base_url:
        raise ValueError("IRS fallback URL is required when fallback is enabled")
    if host.shadow_mode == "state_rehearsal":
        if not host.state_rehearsal_database_url:
            raise ValueError("State Rehearsal requires an isolated database URL")
        if host.state_rehearsal_database_url == profile.core.database_url:
            raise ValueError("State Rehearsal database must differ from the primary database")
        rehearsal_collections = {
            host.state_rehearsal_knowledge_collection,
            host.state_rehearsal_memory_collection,
        }
        if len(rehearsal_collections) != 2 or rehearsal_collections & {
            profile.core.knowledge_milvus_collection,
            profile.core.memory_milvus_collection,
        }:
            raise ValueError("State Rehearsal collections must be isolated")
    if host.cutover_watermark:
        try:
            watermark = host.cutover_watermark_at
        except ValueError as exc:
            raise ValueError("Cutover watermark must be an ISO-8601 datetime") from exc
        if watermark is None or watermark.tzinfo is None:
            raise ValueError("Cutover watermark must include a timezone")


def _csv_values(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())
