from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import Settings
from app.core.memory_runtime import MemoryRuntimePolicy
from app.plugins.knowledge import KnowledgeProvider
from app.schemas.agents import AgentDefinitionV2, CandidateAgentV2
from app.schemas.common import JsonDict
from app.schemas.context import ContextCandidate
from app.schemas.knowledge_provider import (
    KnowledgeProviderRequest,
    KnowledgeRetrievalBudget,
)
from app.schemas.memory import MemoryRecallRequest
from app.schemas.routing import RouteRequest
from app.services.task_continuation import requests_plan_continuation


@dataclass(frozen=True)
class ContextProviderContext:
    request: RouteRequest
    purpose: str
    consumer: str
    candidate_agents: list[CandidateAgentV2] = field(default_factory=list)
    agent: AgentDefinitionV2 | None = None
    sources: JsonDict = field(default_factory=dict)


class ContextProvider(Protocol):
    name: str
    timeout_seconds: float | None

    def applies(self, context: ContextProviderContext) -> bool: ...

    def cache_key(self, context: ContextProviderContext) -> str: ...

    async def collect(
        self, context: ContextProviderContext
    ) -> list[ContextCandidate] | ProviderCollection: ...


@dataclass(frozen=True)
class ProviderCollection:
    candidates: list[ContextCandidate] = field(default_factory=list)
    status: str = "ok"
    error_code: str | None = None
    metadata: JsonDict = field(default_factory=dict)


class BaseContextProvider:
    name = "base"
    timeout_seconds: float | None = None
    cacheable = True

    def applies(self, context: ContextProviderContext) -> bool:
        return True

    def cache_key(self, context: ContextProviderContext) -> str:
        signature = {
            "provider": self.name,
            "request_id": context.request.request_id,
            "user_id": context.request.user.id,
            "tenant_id": context.request.user.tenant_id,
            "query": context.request.input.text,
            "purpose": context.purpose,
            "consumer": context.consumer,
            "agent_id": context.agent.agent_id if context.agent else None,
        }
        digest = hashlib.sha256(
            json.dumps(signature, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return f"{self.name}:{digest}"

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        raise NotImplementedError


class StaticCandidatesProvider(BaseContextProvider):
    cacheable = False

    def __init__(
        self,
        name: str,
        candidates: list[ContextCandidate],
        *,
        status: str = "ok",
        error_code: str | None = None,
    ) -> None:
        self.name = name
        self.candidates = candidates
        self.status = status
        self.error_code = error_code

    async def collect(self, context: ContextProviderContext) -> ProviderCollection:
        return ProviderCollection(
            candidates=[_bind_candidate(item, context) for item in self.candidates],
            status=_provider_status(self.status),
            error_code=self.error_code,
        )


class CurrentInputProvider(BaseContextProvider):
    name = "current_input"

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        request = context.request
        return [
            ContextCandidate(
                candidate_id="current_input",
                source="current_input",
                scope="request",
                role="user",
                content=request.input.text,
                purpose=context.purpose,
                consumers=[context.consumer],
                authority="authoritative",
                visibility=["router"] if context.consumer == "router" else ["agent"],
                source_ref=f"request:{request.request_id or request.session_id}:input",
                priority=100,
                relevance=1.0,
                must_include=True,
                metadata={
                    "request_source": request.source,
                    "attachments_count": len(request.input.attachments),
                    "current_turn": True,
                },
            )
        ]


class CurrentAgentProvider(BaseContextProvider):
    name = "current_agent"

    def applies(self, context: ContextProviderContext) -> bool:
        return context.request.current_agent is not None

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        current = context.request.current_agent
        if current is None:
            return []
        return [
            ContextCandidate(
                candidate_id=f"current_agent:{current.agent_id}",
                source="current_agent",
                scope="agent",
                content=f"Current agent: {current.agent_id}",
                structured_value=current.model_dump(mode="json"),
                purpose=context.purpose,
                consumers=[context.consumer],
                authority="authoritative",
                visibility=["router"] if context.consumer == "router" else ["agent"],
                source_ref=f"agent:{current.agent_id}",
                priority=95,
                relevance=1.0,
                must_include=True,
                metadata={
                    "agent_id": current.agent_id,
                    "agent_session_id": current.agent_session_id,
                    "run_id": current.run_id,
                },
            )
        ]


class FrontendContextProvider(BaseContextProvider):
    name = "frontend_context"

    def applies(self, context: ContextProviderContext) -> bool:
        return bool(context.request.frontend_context)

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        value = context.request.frontend_context
        return [
            ContextCandidate(
                candidate_id="frontend_context",
                source="frontend_context",
                scope="request",
                content=_frontend_summary(value),
                structured_value=value,
                purpose=context.purpose,
                consumers=[context.consumer],
                authority="host_asserted",
                visibility=["router"] if context.consumer == "router" else ["agent"],
                source_ref=f"request:{context.request.request_id or context.request.session_id}:frontend",
                priority=55,
                relevance=0.6,
                metadata={"permission_evidence": False},
            )
        ]


class HistoryProvider(BaseContextProvider):
    name = "history"

    def applies(self, context: ContextProviderContext) -> bool:
        return bool(context.sources.get("host_history") or context.sources.get("agent_history"))

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        candidates: list[ContextCandidate] = []
        for source in ("host_history", "agent_history"):
            values = context.sources.get(source)
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                if not isinstance(value, dict):
                    continue
                message_id = str(value.get("message_id") or f"{source}:{index}")
                role = str(value.get("role") or "") or None
                candidates.append(
                    ContextCandidate(
                        candidate_id=f"{source}:{message_id}",
                        source=source,
                        scope="agent" if source == "agent_history" else "session",
                        role=role,
                        content=str(value.get("content") or ""),
                        purpose=context.purpose,
                        consumers=[context.consumer],
                        authority=(
                            "authoritative"
                            if role == "user"
                            else "model_generated"
                            if role in {"agent", "assistant"}
                            else "derived"
                        ),
                        visibility=["router"] if context.consumer == "router" else ["agent"],
                        source_ref=f"message:{message_id}",
                        dedupe_key=f"message:{message_id}",
                        priority=48 if source == "agent_history" else 40,
                        relevance=0.6,
                        created_at=value.get("created_at"),
                        allowed_agent_ids=(
                            [str(value["agent_id"])]
                            if source == "agent_history" and value.get("agent_id")
                            else []
                        ),
                        metadata={
                            "message_id": message_id,
                            "agent_id": value.get("agent_id"),
                            "agent_session_id": value.get("agent_session_id"),
                            "request_id": value.get("request_id"),
                            "event_id": value.get("event_id"),
                        },
                    )
                )
        return candidates


class ResultProvider(BaseContextProvider):
    name = "results"

    def applies(self, context: ContextProviderContext) -> bool:
        return bool(context.sources.get("recent_results"))

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        values = context.sources.get("recent_results")
        candidates: list[ContextCandidate] = []
        for index, value in enumerate(values if isinstance(values, list) else []):
            if not isinstance(value, dict):
                continue
            result_id = str(value.get("result_id") or f"result:{index}")
            candidates.append(
                ContextCandidate(
                    candidate_id=f"recent_result:{result_id}",
                    source="recent_result",
                    scope="session",
                    content=_result_summary(value),
                    structured_value={
                        "result_id": result_id,
                        "run_id": value.get("run_id"),
                        "agent_id": value.get("agent_id"),
                        "plan_id": value.get("plan_id"),
                        "step_id": value.get("step_id"),
                        "status": value.get("status"),
                        "artifact_refs": value.get("artifact_refs") or [],
                        "summary": _result_summary(value),
                    },
                    purpose=context.purpose,
                    consumers=[context.consumer],
                    authority="authoritative",
                    visibility=["router"] if context.consumer == "router" else ["agent"],
                    source_ref=f"result:{result_id}",
                    dedupe_key=f"result:{result_id}",
                    priority=72,
                    relevance=0.75,
                    created_at=value.get("created_at"),
                    metadata={
                        "result_id": result_id,
                        "run_id": value.get("run_id"),
                        "agent_id": value.get("agent_id"),
                        "status": value.get("status"),
                    },
                )
            )
        return candidates


class EventProvider(BaseContextProvider):
    name = "events"

    def applies(self, context: ContextProviderContext) -> bool:
        return bool(context.sources.get("recent_events"))

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        values = context.sources.get("recent_events")
        candidates: list[ContextCandidate] = []
        for index, value in enumerate(values if isinstance(values, list) else []):
            if not isinstance(value, dict):
                continue
            event_id = str(value.get("event_id") or f"event:{index}")
            candidates.append(
                ContextCandidate(
                    candidate_id=f"recent_event:{event_id}",
                    source="recent_event",
                    scope="session",
                    content=_event_summary(value),
                    structured_value=value,
                    purpose=context.purpose,
                    consumers=[context.consumer],
                    authority="authoritative",
                    visibility=["router"] if context.consumer == "router" else ["agent"],
                    source_ref=f"event:{event_id}",
                    dedupe_key=_event_dedupe_key(value, event_id),
                    priority=74 if value.get("referenced") else 56,
                    relevance=0.8 if value.get("referenced") else 0.55,
                    created_at=value.get("created_at"),
                    metadata={
                        "event_id": event_id,
                        "event_type": value.get("event_type"),
                        "agent_id": value.get("agent_id"),
                        "referenced": bool(value.get("referenced")),
                    },
                )
            )
        return candidates


class PlanProvider(BaseContextProvider):
    name = "plan"

    def applies(self, context: ContextProviderContext) -> bool:
        return bool(context.sources.get("active_plan"))

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        value = context.sources.get("active_plan")
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, dict):
            return []
        plan_id = str(value.get("plan_id") or "active")
        return [
            ContextCandidate(
                candidate_id=f"current_plan:{plan_id}",
                source="current_plan",
                scope="plan",
                content=_plan_summary(value),
                structured_value=value,
                purpose=context.purpose,
                consumers=[context.consumer],
                authority="authoritative",
                visibility=["router"] if context.consumer == "router" else ["agent"],
                source_ref=f"plan:{plan_id}",
                dedupe_key=f"plan:{plan_id}",
                priority=90,
                relevance=0.95,
                must_include=True,
                metadata={"plan_id": plan_id, "status": value.get("status")},
            )
        ]


class EvidenceContextProvider(BaseContextProvider):
    name = "evidence"

    def applies(self, context: ContextProviderContext) -> bool:
        return bool(context.sources.get("evidence"))

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        values = context.sources.get("evidence")
        candidates: list[ContextCandidate] = []
        for index, value in enumerate(values if isinstance(values, list) else []):
            if not isinstance(value, dict):
                continue
            evidence_id = str(value.get("id") or value.get("evidence_id") or f"evidence:{index}")
            score = _bounded_score(
                value.get("score") or value.get("confidence") or value.get("relevance")
            )
            candidates.append(
                ContextCandidate(
                    candidate_id=f"evidence:{evidence_id}",
                    source="evidence",
                    scope="request",
                    content=_evidence_content(value),
                    structured_value=value,
                    purpose=context.purpose,
                    consumers=[context.consumer],
                    authority="derived",
                    visibility=["router"],
                    source_ref=f"evidence:{evidence_id}",
                    dedupe_key=_source_item_key(value, evidence_id),
                    priority=58,
                    relevance=score,
                    confidence=score,
                    metadata={
                        "evidence_id": evidence_id,
                        "source_id": value.get("source_id"),
                        "score": score,
                    },
                )
            )
        return candidates


class ArtifactProvider(BaseContextProvider):
    name = "artifacts"

    def applies(self, context: ContextProviderContext) -> bool:
        values = context.sources.get("recent_results")
        return isinstance(values, list) and any(
            isinstance(value, dict) and value.get("artifact_refs") for value in values
        )

    async def collect(self, context: ContextProviderContext) -> list[ContextCandidate]:
        values = context.sources.get("recent_results")
        candidates: list[ContextCandidate] = []
        seen: set[str] = set()
        for result in values if isinstance(values, list) else []:
            if not isinstance(result, dict):
                continue
            result_id = str(result.get("result_id") or "unknown")
            refs = result.get("artifact_refs")
            for index, raw_ref in enumerate(refs if isinstance(refs, list) else []):
                ref = _artifact_ref(raw_ref, index)
                artifact_id = ref["artifact_id"]
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                value = {**ref, "result_id": result_id}
                candidates.append(
                    ContextCandidate(
                        candidate_id=f"artifact:{artifact_id}",
                        source="artifact",
                        scope="session",
                        content=f"Artifact {artifact_id}: {ref.get('title') or ref.get('type')}",
                        structured_value=value,
                        purpose=context.purpose,
                        consumers=[context.consumer],
                        authority="authoritative",
                        visibility=["router"] if context.consumer == "router" else ["agent"],
                        source_ref=f"artifact:{artifact_id}",
                        dedupe_key=f"artifact:{artifact_id}",
                        priority=70,
                        relevance=0.72,
                        metadata={"artifact_id": artifact_id, "result_id": result_id},
                    )
                )
        return candidates


class MemoryRetrievalProvider(BaseContextProvider):
    cacheable = True

    def __init__(
        self,
        settings: Settings,
        memory_service,
        *,
        stage: str,
        runtime_policy: MemoryRuntimePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_policy = runtime_policy or settings.memory_runtime_policy
        self.memory_service = memory_service
        self.stage = stage
        self.name = f"{stage}_memory"
        self.timeout_seconds = settings.memory_prefetch_timeout_seconds

    def applies(self, context: ContextProviderContext) -> bool:
        if self.memory_service is None:
            return False
        if self.stage == "route":
            return self.runtime_policy.effective_governed_context_memory_enabled
        return bool(
            self.runtime_policy.effective_recall_enabled
            and context.agent
            and context.agent.context.memory.mode == "prefetch"
            and context.agent.context.memory.scopes
        )

    def cache_key(self, context: ContextProviderContext) -> str:
        active_plan = _active_plan_value(context)
        signature = {
            "provider": "memory_retrieval",
            "stage": self.stage,
            "agent_id": context.agent.agent_id if context.agent else None,
            "user_id": context.request.user.id,
            "tenant_id": context.request.user.tenant_id,
            "query": context.request.input.text,
            "scopes": self._scopes(context),
            "active_plan_id": active_plan.get("plan_id") if active_plan else None,
            "active_plan_status": active_plan.get("status") if active_plan else None,
            "policy_version": self.settings.context_policy_version,
        }
        return _retrieval_key(signature)

    def bind_cached(
        self, candidates: list[ContextCandidate], context: ContextProviderContext
    ) -> list[ContextCandidate]:
        return [_bind_candidate(item, context) for item in candidates]

    async def collect(self, context: ContextProviderContext) -> ProviderCollection:
        scopes = self._scopes(context)
        agent_id = context.agent.agent_id if context.agent else None
        max_items = (
            context.agent.context.memory.max_items
            if context.agent and self.stage == "agent"
            else self.settings.memory_default_max_items
        )
        response = await self.memory_service.recall(
            MemoryRecallRequest(
                query=context.request.input.text,
                user=context.request.user,
                scopes=scopes,
                agent_id=agent_id,
                subject_id=context.request.user.id,
                max_items=max_items,
                metadata_filters={"defer_usage_event": True},
            )
        )
        runtime = response.context
        runtime_items = []
        for item in runtime.items:
            if str(item.scope) != "task_memory":
                runtime_items.append(item)
                continue
            if not _matches_active_plan_pointer(item, context):
                continue
            if self.stage == "route":
                # PlanProvider supplies the authoritative canonical projection.
                continue
            runtime_items.append(_canonicalize_task_pointer(item, context))
        candidates = [
            ContextCandidate(
                candidate_id=f"memory:{item.memory_id}",
                source="memory",
                scope="user",
                content=item.content,
                structured_value=item.model_dump(mode="json"),
                purpose=context.purpose,
                consumers=[context.consumer],
                authority="derived",
                visibility=["router"] if context.consumer == "router" else ["agent"],
                source_ref=f"memory:{item.memory_id}",
                dedupe_key=f"memory:{item.memory_id}",
                conflict_key=_memory_conflict_key(item.metadata),
                fact_type=str(item.scope),
                fact_value=item.metadata.get("fact_value"),
                confidence=item.confidence,
                priority=64,
                relevance=item.relevance,
                expires_at=item.ttl_expires_at,
                allowed_agent_ids=[agent_id] if agent_id else [],
                metadata={
                    **item.metadata,
                    "memory_id": item.memory_id,
                    "scope": item.scope,
                    "status": runtime.status,
                },
            )
            for item in runtime_items
        ]
        return ProviderCollection(
            candidates=candidates,
            status=_provider_status(runtime.status),
            error_code=runtime.errors[0] if runtime.errors else None,
            metadata={
                "expired_count": response.expired_count,
                "denied_count": response.denied_count,
            },
        )

    def _scopes(self, context: ContextProviderContext) -> list[str]:
        if self.stage == "agent" and context.agent:
            return [str(item) for item in context.agent.context.memory.scopes]
        return list(self.runtime_policy.route_memory_scopes)


class KnowledgeRetrievalProvider(BaseContextProvider):
    cacheable = True

    def __init__(self, settings: Settings, knowledge_provider: KnowledgeProvider) -> None:
        self.settings = settings
        self.knowledge_provider = knowledge_provider
        self.name = "agent_knowledge"
        self.timeout_seconds = settings.knowledge_prefetch_timeout_seconds

    def applies(self, context: ContextProviderContext) -> bool:
        return bool(context.agent and context.agent.context.knowledge.mode == "prefetch")

    def cache_key(self, context: ContextProviderContext) -> str:
        signature = {
            "provider": "knowledge_retrieval",
            "user_id": context.request.user.id,
            "tenant_id": context.request.user.tenant_id,
            "query": context.request.input.text,
            "source_ids": self._source_ids(context),
            "source_tags": self._source_tags(context),
            "policy_version": self.settings.context_policy_version,
        }
        return _retrieval_key(signature)

    def bind_cached(
        self, candidates: list[ContextCandidate], context: ContextProviderContext
    ) -> list[ContextCandidate]:
        allowed_sources = set(self._source_ids(context))
        rebound = [_bind_candidate(item, context) for item in candidates]
        return [
            item
            for item in rebound
            if not allowed_sources or str(item.metadata.get("source_id")) in allowed_sources
        ]

    async def collect(self, context: ContextProviderContext) -> ProviderCollection:
        source_ids = self._source_ids(context)
        source_tags = self._source_tags(context)
        max_items = (
            context.agent.context.knowledge.max_items
            if context.agent
            else self.settings.knowledge_default_max_items
        )
        response = await self.knowledge_provider.retrieve(
            KnowledgeProviderRequest(
                query=context.request.input.text,
                principal=context.request.user,
                purpose=context.purpose,
                consumer=context.consumer,
                source_ids=source_ids,
                source_tags=source_tags,
                budget=KnowledgeRetrievalBudget(
                    max_items=max_items,
                    timeout_seconds=self.timeout_seconds,
                ),
                trace_context={
                    "request_id": context.request.request_id,
                    "session_id": context.request.session_id,
                },
            )
        )
        status = _provider_status(response.status)
        candidates = [
            ContextCandidate(
                candidate_id=f"knowledge:{item.source_id}:{item.item_id}",
                source="knowledge",
                scope="global",
                content=item.content,
                structured_value=item.model_dump(mode="json"),
                purpose=context.purpose,
                consumers=[context.consumer],
                authority="derived",
                visibility=["router"] if context.consumer == "router" else ["agent"],
                source_ref=f"source:{item.source_id}:{item.item_id}",
                dedupe_key=f"source:{item.source_id}:{item.item_id}",
                confidence=item.score,
                priority=68,
                relevance=item.score,
                allowed_agent_ids=[context.agent.agent_id] if context.agent else [],
                metadata={
                    "item_id": item.item_id,
                    "source_id": item.source_id,
                    "score": item.score,
                    "citation": item.citation.model_dump(mode="json") if item.citation else None,
                    "status": response.status,
                },
            )
            for item in response.items
        ]
        return ProviderCollection(
            candidates=candidates,
            status=status,
            error_code=response.error_code,
            metadata={
                **response.metadata,
                "trace_id": response.trace_id,
                "warnings": response.warnings,
            },
        )

    def _source_ids(self, context: ContextProviderContext) -> list[str]:
        if context.agent:
            return list(context.agent.context.knowledge.source_ids)
        return []

    def _source_tags(self, context: ContextProviderContext) -> list[str]:
        if context.agent:
            return list(context.agent.context.knowledge.source_tags)
        return []


def _bounded_json(value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return rendered[:2000] + ("..." if len(rendered) > 2000 else "")


def _frontend_summary(value: JsonDict) -> str:
    allowed_keys = (
        "page",
        "route",
        "selection",
        "locale",
        "timezone",
        "interaction",
        "ui_state",
    )
    allowed = {key: value[key] for key in allowed_keys if key in value}
    return _bounded_json(allowed)


def _result_summary(value: JsonDict) -> str:
    output = value.get("output")
    if isinstance(output, dict):
        for key in ("summary", "message", "text", "title"):
            item = output.get(key)
            if isinstance(item, str) and item.strip():
                return item[:1000]
    return f"Result {value.get('result_id') or '-'} status: {value.get('status') or 'unknown'}"


def _event_summary(value: JsonDict) -> str:
    return (
        f"Event {value.get('event_id') or '-'}: {value.get('event_type') or 'unknown'} "
        f"status={value.get('status') or 'unknown'}"
    )


def _event_dedupe_key(value: JsonDict, event_id: str) -> str:
    payload = value.get("payload")
    if isinstance(payload, dict) and payload.get("result_id"):
        return f"result:{payload['result_id']}"
    return f"event:{event_id}"


def _plan_summary(value: JsonDict) -> str:
    steps = value.get("steps")
    unfinished = []
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict) or step.get("status") not in {
            "pending",
            "running",
            "blocked",
        }:
            continue
        unfinished.append(f"{step.get('step_id')}: {step.get('description')}")
    if unfinished:
        return "Active plan unfinished steps:\n" + "\n".join(unfinished[:10])
    return f"Plan {value.get('plan_id') or '-'} status: {value.get('status') or 'unknown'}"


def _evidence_content(value: JsonDict) -> str:
    for key in ("content", "text", "snippet", "question"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item
    return _bounded_json(value)


def _bounded_score(value: object) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.65


def _source_item_key(value: JsonDict, fallback: str) -> str:
    source_id = value.get("source_id")
    item_id = value.get("chunk_id") or value.get("item_id") or value.get("id")
    return f"source:{source_id}:{item_id}" if source_id and item_id else f"evidence:{fallback}"


def _artifact_ref(value: object, index: int) -> JsonDict:
    if isinstance(value, str):
        return {"artifact_id": value, "type": "unknown", "uri": value}
    if isinstance(value, dict):
        artifact_id = str(value.get("artifact_id") or value.get("uri") or f"artifact:{index}")
        return {
            "artifact_id": artifact_id,
            "type": str(value.get("type") or "unknown"),
            "uri": str(value.get("uri") or artifact_id),
            "title": value.get("title"),
        }
    return {"artifact_id": f"artifact:{index}", "type": "unknown", "uri": f"artifact:{index}"}


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _retrieval_key(signature: JsonDict) -> str:
    digest = hashlib.sha256(
        json.dumps(signature, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"retrieval:{digest}"


def _bind_candidate(
    candidate: ContextCandidate, context: ContextProviderContext
) -> ContextCandidate:
    return candidate.model_copy(
        update={
            "purpose": context.purpose,
            "consumers": [context.consumer],
            "visibility": ["router"] if context.consumer == "router" else ["agent"],
            "allowed_agent_ids": (
                [context.agent.agent_id]
                if context.agent and context.consumer.startswith("agent:")
                else []
            ),
        }
    )


def _memory_conflict_key(metadata: JsonDict) -> str | None:
    value = metadata.get("conflict_key")
    return str(value) if value else None


def _active_plan_value(context: ContextProviderContext) -> JsonDict | None:
    value = context.sources.get("active_plan")
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        return None
    if value.get("status") not in {"pending", "running", "blocked"}:
        return None
    if value.get("tenant_id") != context.request.user.tenant_id:
        return None
    if value.get("user_id") != context.request.user.id:
        return None
    return value


def _matches_active_plan_pointer(item, context: ContextProviderContext) -> bool:
    active_plan = _active_plan_value(context)
    if active_plan is None:
        return False
    if context.purpose == "route_decision" and not requests_plan_continuation(
        context.request.input.text
    ):
        return False
    structured = item.structured_value
    return bool(
        structured.get("object_type") == "plan"
        and structured.get("plan_id") == active_plan.get("plan_id")
        and structured.get("tenant_id", active_plan.get("tenant_id"))
        == active_plan.get("tenant_id")
        and structured.get("user_id", active_plan.get("user_id")) == active_plan.get("user_id")
    )


def _canonicalize_task_pointer(item, context: ContextProviderContext):
    active_plan = _active_plan_value(context) or {}
    current_step_id = active_plan.get("current_step_id")
    steps = active_plan.get("steps") if isinstance(active_plan.get("steps"), list) else []
    current_step = next(
        (
            value
            for value in steps
            if isinstance(value, dict) and value.get("step_id") == current_step_id
        ),
        None,
    )
    completed = {
        value.get("step_id")
        for value in steps
        if isinstance(value, dict) and value.get("status") == "completed"
    }
    next_step = next(
        (
            value
            for value in steps
            if isinstance(value, dict)
            and value.get("status") == "pending"
            and all(parent in completed for parent in value.get("depends_on", []))
        ),
        None,
    )
    structured = {
        "object_type": "plan",
        "plan_id": active_plan.get("plan_id"),
        "tenant_id": active_plan.get("tenant_id"),
        "user_id": active_plan.get("user_id"),
        "session_id": active_plan.get("session_id"),
        "status": active_plan.get("status"),
        "current_step_id": current_step_id,
        "current_step": current_step,
        "next_step": next_step,
    }
    return item.model_copy(
        update={
            "content": (
                f"Canonical active Plan {active_plan.get('plan_id')} is "
                f"{active_plan.get('status')}; current step: {current_step_id or 'none'}."
            ),
            "structured_value": structured,
            "metadata": {
                **item.metadata,
                "plan_id": active_plan.get("plan_id"),
                "canonical_status": active_plan.get("status"),
                "canonical_current_step_id": current_step_id,
            },
        }
    )


def _provider_status(status: str) -> str:
    return "skipped" if status == "disabled" else status
