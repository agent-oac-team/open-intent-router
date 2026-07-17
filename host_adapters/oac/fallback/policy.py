from dataclasses import dataclass
from enum import StrEnum
from re import Pattern, compile


class OperationClass(StrEnum):
    READ_ONLY = "read_only"
    ROUTE_STATEFUL = "route_stateful"
    CONTROL_WRITE = "control_write"
    RUNTIME_WRITE = "runtime_write"


class CommitStatus(StrEnum):
    NOT_ACCEPTED = "not_accepted"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdapterOperation:
    name: str
    method: str
    path_pattern: Pattern[str]
    operation_class: OperationClass

    def matches(self, method: str, path: str) -> bool:
        return self.method == method.upper() and self.path_pattern.fullmatch(path) is not None


def _operation(name: str, method: str, path: str, kind: OperationClass) -> AdapterOperation:
    return AdapterOperation(name, method, compile(path), kind)


ADAPTER_OPERATIONS = (
    _operation("central.route", "POST", r"/api/v1/central/route", OperationClass.ROUTE_STATEFUL),
    _operation(
        "central.navigation_event",
        "POST",
        r"/api/v1/central/events/navigation",
        OperationClass.RUNTIME_WRITE,
    ),
    _operation(
        "central.agent_event", "POST", r"/api/v1/central/events/agent", OperationClass.RUNTIME_WRITE
    ),
    _operation(
        "central.plan_confirm",
        "POST",
        r"/api/v1/central/plans/[^/]+/confirm",
        OperationClass.RUNTIME_WRITE,
    ),
    _operation("registry.list", "GET", r"/api/v1/admin/agent-registry", OperationClass.READ_ONLY),
    _operation(
        "registry.create", "POST", r"/api/v1/admin/agent-registry", OperationClass.CONTROL_WRITE
    ),
    _operation(
        "registry.update",
        "PUT",
        r"/api/v1/admin/agent-registry/[^/]+",
        OperationClass.CONTROL_WRITE,
    ),
    _operation(
        "registry.enabled",
        "PATCH",
        r"/api/v1/admin/agent-registry/[^/]+/enabled",
        OperationClass.CONTROL_WRITE,
    ),
    _operation(
        "registry.delete",
        "DELETE",
        r"/api/v1/admin/agent-registry/[^/]+",
        OperationClass.CONTROL_WRITE,
    ),
    _operation("knowledge.search", "POST", r"/api/v1/knowledge/search", OperationClass.READ_ONLY),
    _operation(
        "knowledge.grouped_search",
        "POST",
        r"/api/v1/knowledge/grouped-search",
        OperationClass.READ_ONLY,
    ),
    _operation("knowledge.read", "POST", r"/api/v1/knowledge/read", OperationClass.READ_ONLY),
    _operation("knowledge.assets", "GET", r"/api/v1/knowledge/assets", OperationClass.READ_ONLY),
    _operation(
        "knowledge.asset", "GET", r"/api/v1/knowledge/assets/[^/]+", OperationClass.READ_ONLY
    ),
    _operation(
        "knowledge.asset_chunks",
        "GET",
        r"/api/v1/knowledge/assets/[^/]+/chunks",
        OperationClass.READ_ONLY,
    ),
    _operation(
        "knowledge.chunk", "GET", r"/api/v1/knowledge/chunks/[^/]+", OperationClass.READ_ONLY
    ),
    _operation(
        "knowledge_admin.upload",
        "POST",
        r"/api/v1/admin/knowledge/files",
        OperationClass.CONTROL_WRITE,
    ),
    _operation(
        "knowledge_admin.list", "GET", r"/api/v1/admin/knowledge/files", OperationClass.READ_ONLY
    ),
    _operation(
        "knowledge_admin.detail",
        "GET",
        r"/api/v1/admin/knowledge/files/[^/]+",
        OperationClass.READ_ONLY,
    ),
    _operation(
        "knowledge_admin.chunks",
        "GET",
        r"/api/v1/admin/knowledge/files/[^/]+/chunks",
        OperationClass.READ_ONLY,
    ),
    _operation(
        "knowledge_admin.delete",
        "DELETE",
        r"/api/v1/admin/knowledge/files/[^/]+",
        OperationClass.CONTROL_WRITE,
    ),
    _operation(
        "knowledge_admin.retry",
        "POST",
        r"/api/v1/admin/knowledge/files/[^/]+/retry",
        OperationClass.CONTROL_WRITE,
    ),
)


def classify_operation(method: str, path: str) -> AdapterOperation:
    matches = [item for item in ADAPTER_OPERATIONS if item.matches(method, path)]
    if len(matches) != 1:
        raise KeyError(f"Adapter operation is not uniquely classified: {method} {path}")
    return matches[0]


def write_fence_blocked(host_settings, operation: AdapterOperation) -> bool:
    if not host_settings.write_fence_enabled:
        return False
    if host_settings.write_freeze_enabled:
        return operation.operation_class in {
            OperationClass.CONTROL_WRITE,
            OperationClass.RUNTIME_WRITE,
            OperationClass.ROUTE_STATEFUL,
        }
    if operation.operation_class == OperationClass.CONTROL_WRITE:
        return not host_settings.oir_control_write_enabled
    if operation.operation_class == OperationClass.RUNTIME_WRITE:
        return not host_settings.oir_runtime_write_enabled
    return False
