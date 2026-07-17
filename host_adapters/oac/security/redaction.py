import re
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(^|_)(authorization|credential|execution_ticket|lease_token|password|secret|signature|ticket|token)($|_)",
    re.IGNORECASE,
)
INLINE_SECRET = re.compile(
    r"(?i)\b(execution_ticket|lease_token|token|authorization|secret)=([^\s&,]+)"
)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value
