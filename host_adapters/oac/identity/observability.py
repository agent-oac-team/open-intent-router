from collections import Counter
from dataclasses import dataclass, field
from threading import Lock

from host_adapters.oac.identity.profiles import CREDENTIAL_PROFILES

_OPERATIONS = frozenset({"registry", "route", "runtime", "unknown"})
_OUTCOMES = frozenset({"verified", "authentication_failed", "authorization_failed"})


@dataclass
class HostSignatureMetrics:
    counters: Counter[tuple[str, str, str, str]] = field(default_factory=Counter)
    _lock: Lock = field(default_factory=Lock)

    def record(
        self,
        *,
        version: str,
        credential_class: str,
        operation: str,
        outcome: str,
    ) -> None:
        safe_version = version if version in {"v1", "v2"} else "unknown"
        safe_class = credential_class if credential_class in CREDENTIAL_PROFILES else "unknown"
        safe_operation = operation if operation in _OPERATIONS else "unknown"
        safe_outcome = outcome if outcome in _OUTCOMES else "authentication_failed"
        with self._lock:
            self.counters[(safe_version, safe_class, safe_operation, safe_outcome)] += 1

    def snapshot(self) -> list[dict[str, str | int]]:
        with self._lock:
            items = sorted(self.counters.items())
        return [
            {
                "signature_version": version,
                "credential_class": credential_class,
                "operation": operation,
                "outcome": outcome,
                "count": count,
            }
            for (version, credential_class, operation, outcome), count in items
        ]

    def clear(self) -> None:
        with self._lock:
            self.counters.clear()


HOST_SIGNATURE_METRICS = HostSignatureMetrics()


def classify_host_request(method: str, path: str) -> str:
    if path.startswith("/api/v1/admin/agent-registry"):
        return "registry"
    if path == "/api/v1/central/route":
        return "route"
    if path.startswith("/api/v1/central"):
        return "runtime"
    return "unknown"
