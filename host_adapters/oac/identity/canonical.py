from urllib.parse import parse_qsl, quote, unquote

from host_adapters.oac.identity.models import SignedHostRequest


def canonicalize_host_request(request: SignedHostRequest, *, tenant_id: str) -> str:
    groups = sorted({item.strip() for item in request.groups.split(",") if item.strip()})
    roles = sorted({item.strip() for item in request.roles.split(",") if item.strip()})
    return "\n".join(
        [
            "OIR-HOST-V2",
            request.key_id,
            request.audience,
            request.timestamp,
            request.nonce,
            request.method.upper(),
            normalize_path(request.path),
            normalize_query(request.query),
            request.content_sha256.lower(),
            request.principal_type,
            tenant_id,
            request.user_id,
            ",".join(roles),
            ",".join(groups),
            request.active_bundle_id,
            request.claims_version,
            request.policy_version,
            request.credential_class,
        ]
    )


def normalize_path(path: str) -> str:
    normalized = quote(unquote(path or "/"), safe="/-._~")
    return normalized if normalized.startswith("/") else f"/{normalized}"


def normalize_query(query: str) -> str:
    encoded = [
        (quote(key, safe="-._~"), quote(value, safe="-._~"))
        for key, value in parse_qsl(query, keep_blank_values=True)
    ]
    encoded.sort()
    return "&".join(f"{key}={value}" for key, value in encoded)
