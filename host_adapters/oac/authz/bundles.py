import re
from dataclasses import dataclass

from app.schemas.common import normalize_entitlements

OAC_CLAIMS_VERSION = "oac-principal-v1"
OAC_POLICY_VERSION = "oac-authz-v1"
SAFE_BUNDLE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class EntitlementBundle:
    bundle_id: str
    legacy_tag: str
    grants: tuple[str, ...]


class BundleCatalog:
    def __init__(self, *, policy_version: str, bundles: tuple[EntitlementBundle, ...]) -> None:
        self.policy_version = policy_version
        self.bundles = bundles
        self._validate()
        self.by_id = {bundle.bundle_id: bundle for bundle in bundles}
        self.by_tag = {bundle.legacy_tag: bundle for bundle in bundles}
        self.by_entitlement = {
            entitlement: bundle for bundle in bundles for entitlement in bundle.grants
        }

    def _validate(self) -> None:
        if not self.policy_version.isascii() or not self.policy_version:
            raise ValueError("bundle policy version must be non-empty ASCII")
        ids = [bundle.bundle_id for bundle in self.bundles]
        tags = [bundle.legacy_tag for bundle in self.bundles]
        grants = [grant for bundle in self.bundles for grant in bundle.grants]
        if len(ids) != len(set(ids)) or len(tags) != len(set(tags)):
            raise ValueError("bundle IDs and legacy tags must be unique")
        if len(grants) != len(set(grants)):
            raise ValueError("bundle grants must be reversible")
        for bundle in self.bundles:
            if not SAFE_BUNDLE_ID.fullmatch(bundle.bundle_id):
                raise ValueError("bundle ID is invalid")
            normalized = normalize_entitlements(bundle.grants)
            if not normalized or tuple(normalized) != bundle.grants:
                raise ValueError("bundle grants must be non-empty and normalized")
            if any(not grant.endswith(".access") for grant in bundle.grants):
                raise ValueError("OAC bundles may only grant .access entitlements")

    def entitlements_for_tag(self, tag: str) -> tuple[str, ...]:
        return self.by_tag[tag].grants

    def entitlements_for_bundle(self, bundle_id: str) -> tuple[str, ...]:
        return self.by_id[bundle_id].grants

    def bundle_for_tag(self, tag: str) -> EntitlementBundle:
        return self.by_tag[tag]

    def legacy_tag_for_entitlement(self, entitlement: str) -> str:
        return self.by_entitlement[entitlement].legacy_tag


OAC_BUNDLE_CATALOG = BundleCatalog(
    policy_version=OAC_POLICY_VERSION,
    bundles=(
        EntitlementBundle(
            bundle_id="oac-operations",
            legacy_tag="运营版",
            grants=("workspace.operations.access",),
        ),
        EntitlementBundle(
            bundle_id="oac-sales-enablement",
            legacy_tag="展业版",
            grants=("workspace.sales_enablement.access",),
        ),
    ),
)
