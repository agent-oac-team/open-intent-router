import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from host_adapters.oac.fallback.policy import AdapterOperation, OperationClass


class ShadowSideEffectBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class StructuralDiff:
    diff_id: str
    fingerprint: str
    severity: str
    blocking: bool
    approved: bool
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "fingerprint": self.fingerprint,
            "severity": self.severity,
            "blocking": self.blocking,
            "approved": self.approved,
            "details": self.details,
        }


class DecisionShadowGuard:
    def assert_allowed(self, operation: AdapterOperation, *, side_effect_free: bool) -> None:
        if operation.operation_class in {
            OperationClass.CONTROL_WRITE,
            OperationClass.RUNTIME_WRITE,
        }:
            raise ShadowSideEffectBlocked(f"Shadow write blocked: {operation.name}")
        if operation.operation_class == OperationClass.ROUTE_STATEFUL and not side_effect_free:
            raise ShadowSideEffectBlocked("Shadow Route requires a side-effect-free decision port")


def route_diff(irs: dict, oir: dict, *, approved: set[str] | None = None) -> list[StructuralDiff]:
    fields = {
        "action": (_nested(irs, "route", "action"), _nested(oir, "route", "action")),
        "agent_id": (_nested(irs, "route", "agent_id"), _nested(oir, "route", "agent_id")),
        "relation": (_nested(irs, "context", "relation"), _nested(oir, "context", "relation")),
        "plan": (_plan_shape(irs.get("plan")), _plan_shape(oir.get("plan"))),
        "message_type": (
            _message_type(_nested(irs, "route", "message")),
            _message_type(_nested(oir, "route", "message")),
        ),
    }
    return _build_diffs("route", fields, blocking_fields={"action", "agent_id"}, approved=approved)


class ShadowReplayRunner:
    def __init__(
        self,
        *,
        repository,
        guard: DecisionShadowGuard | None = None,
        approved_fingerprints: set[str] | None = None,
        metrics=None,
    ) -> None:
        self.repository = repository
        self.guard = guard or DecisionShadowGuard()
        self.approved_fingerprints = approved_fingerprints or set()
        self.metrics = metrics

    async def run(
        self,
        dataset: dict,
        *,
        operation_resolver: Callable[[dict], AdapterOperation],
        oir_executor: Callable[[dict], Awaitable[dict]],
    ) -> dict:
        samples = dataset.get("samples", [])
        await self.repository.save_dataset(
            {
                "dataset_id": dataset["dataset_id"],
                "version": dataset["version"],
                "sample_count": len(samples),
                "metadata": dataset.get("metadata", {}),
            }
        )
        blocking = 0
        categories: dict[str, dict[str, int]] = {}
        for sample in samples:
            operation = operation_resolver(sample)
            self.guard.assert_allowed(
                operation,
                side_effect_free=bool(sample.get("side_effect_free", False)),
            )
            started = time.perf_counter()
            oir_result = await oir_executor(sample)
            latency_ms = (time.perf_counter() - started) * 1000
            irs_result = sample["irs_result"]
            if sample["kind"] != "route":
                raise ValueError("OAC shadow replay supports Central Route samples only")
            diffs = route_diff(irs_result, oir_result, approved=self.approved_fingerprints)
            blocking += sum(item.blocking and not item.approved for item in diffs)
            if self.metrics is not None:
                self.metrics.counters[f"shadow:{operation.name}:processed"] += 1
                self.metrics.counters["shadow:diff"] += len(diffs)
                self.metrics.counters["shadow:blocking_diff"] += sum(
                    item.blocking and not item.approved for item in diffs
                )
            category = str(sample.get("category") or sample["kind"])
            category_status = categories.setdefault(category, {"total": 0, "processed": 0})
            category_status["total"] += 1
            category_status["processed"] += 1
            result = {
                "result_id": f"shadow_result_{uuid4().hex}",
                "dataset_id": dataset["dataset_id"],
                "sample_id": sample["sample_id"],
                "operation": operation.name,
                "irs_result": irs_result,
                "oir_result": oir_result,
                "irs_latency_ms": sample.get("irs_latency_ms", 0),
                "oir_latency_ms": latency_ms,
            }
            await self.repository.save_result(result, [item.as_dict() for item in diffs])
        total = len(samples)
        return {
            "contract": "oac-oir-shadow-replay/v1",
            "dataset_id": dataset["dataset_id"],
            "dataset_version": dataset["version"],
            "sample_count": total,
            "processed_count": total,
            "coverage": 1.0 if total else 0.0,
            "blocking_diff_count": blocking,
            "category_coverage": {
                name: {
                    **counts,
                    "coverage": counts["processed"] / counts["total"],
                }
                for name, counts in sorted(categories.items())
            },
            "passed": total > 0 and blocking == 0,
        }


def _build_diffs(
    kind: str,
    fields: dict[str, tuple[Any, Any]],
    *,
    blocking_fields: set[str],
    approved: set[str] | None,
) -> list[StructuralDiff]:
    results = []
    for field, (irs, oir) in fields.items():
        if irs == oir:
            continue
        details = {"kind": kind, "field": field, "irs": irs, "oir": oir}
        encoded = json.dumps(details, ensure_ascii=False, sort_keys=True, default=str).encode()
        fingerprint = hashlib.sha256(encoded).hexdigest()
        blocking = field in blocking_fields
        results.append(
            StructuralDiff(
                diff_id=f"shadow_diff_{fingerprint[:32]}",
                fingerprint=fingerprint,
                severity="blocking" if blocking else "warning",
                blocking=blocking,
                approved=fingerprint in (approved or set()),
                details=details,
            )
        )
    return results


def _nested(value: dict, *keys: str):
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _plan_shape(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        "current_step": value.get("current_step"),
        "steps": [
            {
                "agent_id": item.get("agent_id"),
                "status": item.get("status"),
            }
            for item in value.get("steps", [])
            if isinstance(item, dict)
        ],
    }


def _message_type(value: Any) -> str:
    return "empty" if not isinstance(value, str) or not value.strip() else "text"
