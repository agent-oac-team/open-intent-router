import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "token",
    "secret",
    "password",
    "x-api-key",
    "credential",
    "credentials",
    "connection_string",
    "database_url",
    "dsn",
    "prompt",
    "quote",
    "candidate_quote",
    "user_text",
    "assistant_text",
}

_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "password", "secret", "credential")
_TEXT_PATTERNS = (
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+)(?::[^@\s/]*)?@"),
    re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret|authorization)\b"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    re.compile(r"(?<!\d)(?:\d[ -]?){15,18}[0-9Xx](?!\d)"),
)


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower()
    return (
        normalized in SENSITIVE_KEYS
        or normalized.endswith("_dsn")
        or normalized.endswith(("_token", "_prompt", "_quote"))
        or any(part in normalized for part in _SENSITIVE_KEY_PARTS)
    )


def redact_text(value: str, *, max_length: int | None = None) -> str:
    redacted = value
    for index, pattern in enumerate(_TEXT_PATTERNS):
        replacement = r"\1***:***@" if index == 0 else "***REDACTED***"
        redacted = pattern.sub(replacement, redacted)
    if max_length is not None and len(redacted) > max_length:
        redacted = f"{redacted[:max_length]}...[truncated]"
    return redacted


def redact_connection_location(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "***REDACTED***"
    if not parsed.scheme or not parsed.netloc:
        return redact_text(value)
    if not hostname:
        return "***REDACTED***"
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                "***REDACTED***"
                if is_sensitive_key(key)
                else redact_connection_location(item)
                if isinstance(item, str) and str(key).lower().endswith(("_uri", "_url", "base_url"))
                else redact_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
