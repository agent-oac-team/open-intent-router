import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.core.config import Settings
from app.schemas.common import UserContext


@dataclass
class EvidenceResult:
    intent_hint: str | None = None
    candidate_agent_ids: list[str] = field(default_factory=list)
    route_override: dict[str, Any] | None = None
    route_override_denied: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class EvidenceProvider(Protocol):
    async def match(
        self,
        *,
        question: str,
        candidate_agent_ids: list[str],
        user: UserContext,
    ) -> EvidenceResult: ...


class NoopEvidenceProvider:
    async def match(
        self,
        *,
        question: str,
        candidate_agent_ids: list[str],
        user: UserContext,
    ) -> EvidenceResult:
        return EvidenceResult(metadata={"provider": "noop", "status": "skipped"})


class FileFixedQuestionEvidenceProvider:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    async def match(
        self,
        *,
        question: str,
        candidate_agent_ids: list[str],
        user: UserContext,
    ) -> EvidenceResult:
        if not self.path.exists():
            return EvidenceResult(errors=[f"Fixed-question file not found: {self.path}"])
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        items = data.get("fixed_questions") or []
        normalized = _normalize(question)
        for item in items:
            configured = _normalize(str(item.get("question", "")))
            match_type = item.get("match_type", "exact")
            if not _matches(normalized, configured, match_type):
                continue
            configured_ids = [str(agent_id) for agent_id in item.get("candidate_agent_ids", [])]
            mapped_ids = list(configured_ids)
            route_override = item.get("route_override")
            target_agent_id = None
            if isinstance(route_override, dict) and route_override.get("target_agent_id"):
                target_agent_id = str(route_override["target_agent_id"])
                mapped_ids.append(target_agent_id)
            mapped_ids = list(
                dict.fromkeys(
                    agent_id for agent_id in mapped_ids if agent_id in candidate_agent_ids
                )
            )
            strength = item.get("strength", "weak")
            route_override_denied = None
            if (
                strength == "strong"
                and isinstance(route_override, dict)
                and target_agent_id
                and target_agent_id not in candidate_agent_ids
            ):
                route_override_denied = {
                    **route_override,
                    "target_agent_id": target_agent_id,
                    "reason": "permission_denied",
                }
                route_override = None
            elif not (
                strength == "strong"
                and isinstance(route_override, dict)
                and target_agent_id
                and target_agent_id in candidate_agent_ids
            ):
                route_override = None
            evidence = [
                {
                    "type": "fixed_question",
                    "question": item.get("question"),
                    "strength": strength,
                    "matched_agent_ids": mapped_ids,
                    "configured_agent_ids": configured_ids,
                    "route_override_target_agent_id": target_agent_id,
                    "route_override_denied": route_override_denied is not None,
                }
            ]
            return EvidenceResult(
                intent_hint=item.get("intent_hint"),
                candidate_agent_ids=mapped_ids,
                route_override=route_override if strength == "strong" else None,
                route_override_denied=route_override_denied,
                evidence=evidence,
                metadata={
                    "provider": "fixed_question",
                    "status": "matched",
                    "strength": strength,
                },
            )
        return EvidenceResult(metadata={"provider": "fixed_question", "status": "no_match"})


class EvidenceProviderScheduler:
    def __init__(
        self,
        providers: list[tuple[str, EvidenceProvider]],
        *,
        timeout_seconds: float,
    ) -> None:
        self.providers = providers
        self.timeout_seconds = timeout_seconds

    async def match(
        self,
        *,
        question: str,
        candidate_agent_ids: list[str],
        user: UserContext,
    ) -> EvidenceResult:
        scheduled: list[dict[str, Any]] = []
        merged = EvidenceResult(metadata={"scheduler": "evidence_provider_scheduler"})
        for name, provider in self.providers:
            scheduled.append({"provider": name, "status": "selected"})
            try:
                result = await asyncio.wait_for(
                    provider.match(
                        question=question,
                        candidate_agent_ids=candidate_agent_ids,
                        user=user,
                    ),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError:
                merged.errors.append(f"Evidence provider timed out: {name}")
                scheduled[-1]["status"] = "timeout"
                continue
            except Exception as exc:
                merged.errors.append(f"Evidence provider failed: {name}: {exc}")
                scheduled[-1]["status"] = "error"
                continue
            scheduled[-1]["result"] = result.metadata
            _merge_result(merged, result)
            if result.route_override or result.route_override_denied:
                break
        merged.metadata["scheduled_providers"] = scheduled
        return merged


def build_evidence_provider(settings: Settings) -> EvidenceProvider:
    if not settings.evidence_provider_enabled:
        return NoopEvidenceProvider()
    return EvidenceProviderScheduler(
        [
            (
                "fixed_question",
                FileFixedQuestionEvidenceProvider(settings.evidence_fixed_questions_path),
            )
        ],
        timeout_seconds=settings.evidence_provider_timeout_seconds,
    )


def _merge_result(target: EvidenceResult, source: EvidenceResult) -> None:
    if source.intent_hint and not target.intent_hint:
        target.intent_hint = source.intent_hint
    target.candidate_agent_ids.extend(
        agent_id
        for agent_id in source.candidate_agent_ids
        if agent_id not in target.candidate_agent_ids
    )
    if source.route_override and not target.route_override:
        target.route_override = source.route_override
    if source.route_override_denied and not target.route_override_denied:
        target.route_override_denied = source.route_override_denied
    target.evidence.extend(source.evidence)
    target.errors.extend(source.errors)


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _matches(question: str, configured: str, match_type: str) -> bool:
    if not configured:
        return False
    if match_type == "contains":
        return configured in question
    return question == configured
