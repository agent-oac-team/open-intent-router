import base64
import hashlib
import hmac
import json


def native_principal_headers(
    *,
    secret: str,
    subject: str = "user-1",
    tenant: str = "tenant-1",
    roles: list[str] | None = None,
    groups: list[str] | None = None,
    entitlements: list[str] | None = None,
    attributes: dict | None = None,
    signed: bool = True,
) -> dict[str, str]:
    claims = {
        "claims_version": "oir-principal-v1",
        "subject": subject,
        "tenant": tenant,
        "roles": roles or [],
        "groups": groups or [],
        "entitlements": entitlements or [],
        "attributes": attributes or {},
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    headers = {"X-OIR-Principal-Envelope": encoded}
    if signed:
        headers["X-OIR-Principal-Signature"] = hmac.new(
            secret.encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
    return headers
