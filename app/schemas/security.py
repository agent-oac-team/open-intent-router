from typing import Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import JsonDict, StrictBaseModel, UserContext, normalize_entitlements


class NativePrincipal(StrictBaseModel):
    claims_version: Literal["oir-principal-v1"]
    subject: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(alias="tenant", min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    entitlements: list[str] = Field(default_factory=list)
    attributes: JsonDict = Field(default_factory=dict)

    @field_validator("roles", "groups", mode="before")
    @classmethod
    def normalize_string_claims(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list | tuple | set | frozenset):
            raise ValueError("Principal collection claims must be arrays")
        normalized: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise ValueError("Principal collection claims must contain strings")
            item = item.strip()
            if item:
                normalized.add(item)
        return sorted(normalized)

    @field_validator("entitlements", mode="before")
    @classmethod
    def normalize_principal_entitlements(cls, value: Any) -> list[str]:
        return normalize_entitlements(value)

    def to_user_context(self) -> UserContext:
        attributes = {
            key: value
            for key, value in self.attributes.items()
            if key not in {"tenant", "tenant_id"}
        }
        attributes["tenant_id"] = self.tenant_id
        return UserContext(
            id=self.subject,
            roles=self.roles,
            groups=self.groups,
            entitlements=self.entitlements,
            attributes=attributes,
        )
