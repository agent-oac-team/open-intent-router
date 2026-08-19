from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import Settings, get_settings
from host_adapters.oac.authz import OAC_CLAIMS_VERSION, OAC_POLICY_VERSION

HostEnvironment = Literal["local", "test"]
ShadowMode = Literal["off", "decision", "state_rehearsal"]


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
    authz_policy_version: str = "oac-authz-v1"
    claims_version: str = "oac-principal-v1"
    admin_claims_version: str = "oac-admin-principal-v1"
    admin_policy_version: str = "oac-control-v1"
    coze_claims_version: str = "oac-service-principal-v1"
    coze_policy_version: str = "oac-readonly-v1"

    shadow_mode: ShadowMode = "off"
    write_fence_enabled: bool = True
    write_freeze_enabled: bool = False
    oir_control_write_enabled: bool = True
    oir_runtime_write_enabled: bool = True
    state_rehearsal_database_url: str | None = None
    state_rehearsal_memory_collection: str = "oir_memory_vectors_rehearsal"
    cutover_watermark: str | None = None
    cutover_audit_path: str = ".data/oac_cutover_quarantine.jsonl"
    enforce_registry_single_writer: bool = False
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
    external_executor_refs: str = ""
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

    @property
    def supported_external_executor_refs(self) -> frozenset[str]:
        """Explicit Host-owned logical External Executor capability identifiers."""

        return _csv_values(self.external_executor_refs)


def memory_execution_plane_for_shadow(mode: ShadowMode) -> str:
    return {
        "off": "live",
        "decision": "decision_shadow",
        "state_rehearsal": "state_rehearsal",
    }[mode]


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
    validate_host_credential_profiles(profile)
    return profile


def validate_registry_single_writer(profile: OacHostProfile) -> None:
    if not profile.host.enforce_registry_single_writer:
        return
    if (
        profile.core.registry_backend != "database"
        or profile.core.registry_file_fallback_on_empty
        or profile.host.feishu_registry_sync_enabled
        or profile.host.registry_file_restore_enabled
    ):
        raise ValueError("OIR database must be the only writable Registry source")


def validate_governance_profile(profile: OacHostProfile) -> None:
    host = profile.host
    if host.claims_version != OAC_CLAIMS_VERSION or host.authz_policy_version != OAC_POLICY_VERSION:
        raise ValueError("OAC claims or authorization policy version is unsupported")
    if host.write_fence_enabled:
        if not host.write_freeze_enabled and (
            not host.oir_control_write_enabled or not host.oir_runtime_write_enabled
        ):
            raise ValueError("OIR must be the only writable primary outside an explicit freeze")
    if host.supported_external_executor_refs:
        host_ticket_secret = (
            host.execution_ticket_secret.get_secret_value()
            if host.execution_ticket_secret is not None
            else None
        )
        if not (host_ticket_secret or profile.core.execution_ticket_secret):
            raise ValueError(
                "External Execution requires OAC_HOST_EXECUTION_TICKET_SECRET "
                "or EXECUTION_TICKET_SECRET"
            )
    if host.shadow_mode == "state_rehearsal":
        if not host.state_rehearsal_database_url:
            raise ValueError("State Rehearsal requires an isolated database URL")
        if host.state_rehearsal_database_url == profile.core.database_url:
            raise ValueError("State Rehearsal database must differ from the primary database")
        if host.state_rehearsal_memory_collection == profile.core.memory_milvus_collection:
            raise ValueError("State Rehearsal Memory collection must be isolated")
    if host.cutover_watermark:
        try:
            watermark = host.cutover_watermark_at
        except ValueError as exc:
            raise ValueError("Cutover watermark must be an ISO-8601 datetime") from exc
        if watermark is None or watermark.tzinfo is None:
            raise ValueError("Cutover watermark must include a timezone")


def validate_host_credential_profiles(profile: OacHostProfile) -> None:
    host = profile.host
    expected_versions = {
        "user": ("oac-principal-v1", "oac-authz-v1"),
        "admin": ("oac-admin-principal-v1", "oac-control-v1"),
        "coze": ("oac-service-principal-v1", "oac-readonly-v1"),
    }
    actual_versions = {
        "user": (host.claims_version, host.authz_policy_version),
        "admin": (host.admin_claims_version, host.admin_policy_version),
        "coze": (host.coze_claims_version, host.coze_policy_version),
    }
    if actual_versions != expected_versions:
        raise ValueError("OAC Host credential profile versions are unsupported")

    current_credentials = {
        "oac_user": (host.identity_current_key_id, host.identity_current_key),
        "oac_admin": (host.oac_admin_key_id, host.oac_admin_credential),
        "coze_workflow": (host.coze_workflow_key_id, host.coze_workflow_credential),
    }
    if any(not key_id or credential is None for key_id, credential in current_credentials.values()):
        raise ValueError("all current OAC Host credential profiles require a key")
    key_ids = [key_id for key_id, _ in current_credentials.values()]
    if len(set(key_ids)) != len(key_ids):
        raise ValueError("OAC Host key IDs cannot cross credential classes")


def _csv_values(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())
