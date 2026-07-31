import re
from collections.abc import Mapping
from typing import Any

_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_CONTEXT_STATUSES = frozenset({"disabled", "ok", "empty", "timeout", "error", "denied"})


def sanitize_persisted_knowledge(value: Any) -> Any:
    """Recursively replace Knowledge Context bodies with audit-safe references."""
    if isinstance(value, list):
        return [sanitize_persisted_knowledge(item) for item in value]
    if not isinstance(value, Mapping):
        return value

    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "knowledge_context" and isinstance(item, Mapping):
            sanitized[key] = knowledge_reference_projection(item)
        else:
            sanitized[str(key)] = sanitize_persisted_knowledge(item)
    return sanitized


def knowledge_reference_projection(context: Mapping[str, Any]) -> dict[str, Any]:
    items = _item_references(context.get("items"))
    source_ids = _references(context.get("source_ids"))
    for item in items:
        source_id = item.get("source_id")
        if isinstance(source_id, str) and source_id not in source_ids:
            source_ids.append(source_id)

    citations = _citation_references(context.get("citations"))
    for citation in citations:
        source_id = citation["source_id"]
        if source_id not in source_ids:
            source_ids.append(source_id)

    metadata = context.get("metadata")
    trace_id = metadata.get("trace_id") if isinstance(metadata, Mapping) else None
    if not _is_reference(trace_id):
        direct_trace_id = context.get("trace_id")
        trace_id = direct_trace_id if _is_reference(direct_trace_id) else None

    projection: dict[str, Any] = {
        "status": _safe_status(context.get("status")),
        "item_count": len(items),
        "items": items,
        "citations": citations,
        "source_ids": source_ids,
        "truncated": bool(context.get("truncated", False)),
        "errors": _error_codes(context.get("errors")),
    }
    if trace_id:
        projection["trace_id"] = trace_id
        # Keep the stable field order used by serialized audit fixtures.
        projection = {
            "status": projection.pop("status"),
            "trace_id": projection.pop("trace_id"),
            **projection,
        }
    return projection


def _item_references(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        item_id = item.get("item_id")
        source_id = item.get("source_id")
        if not _is_reference(item_id):
            continue
        reference = {"item_id": item_id}
        if _is_reference(source_id):
            reference["source_id"] = source_id
        references.append(reference)
    return references


def _citation_references(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    for citation in value:
        if not isinstance(citation, Mapping):
            continue
        source_id = citation.get("source_id")
        if _is_reference(source_id):
            reference = {"source_id": source_id}
            if reference not in references:
                references.append(reference)
    return references


def _references(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if _is_reference(item)))


def _safe_status(value: Any) -> str:
    return value if value in _CONTEXT_STATUSES else "error"


def _error_codes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item for item in value if isinstance(item, str) and _ERROR_CODE_PATTERN.fullmatch(item)
        )
    )


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and _REFERENCE_PATTERN.fullmatch(value) is not None
