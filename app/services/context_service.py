import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from math import ceil
from uuid import uuid4

from app.core.config import Settings
from app.core.memory_runtime import MemoryRuntimePolicy
from app.core.redaction import redact_value
from app.schemas.agent_context import MemoryContextItem
from app.schemas.common import JsonDict, normalize_artifact_refs
from app.schemas.context import (
    ContextAssemblySession,
    ContextBudget,
    ContextItem,
    ContextPack,
    ContextSelectionSummary,
    ContextUsage,
)
from app.schemas.plans import Plan
from app.schemas.routing import RouteContext, RouteRequest
from app.services.context_pipeline_service import ContextPipelineService
from app.services.context_providers import (
    ArtifactProvider,
    CurrentAgentProvider,
    CurrentInputProvider,
    EventProvider,
    EvidenceContextProvider,
    FrontendContextProvider,
    HistoryProvider,
    MemoryRetrievalProvider,
    PlanProvider,
    ResultProvider,
)


class ContextService:
    def __init__(
        self,
        settings: Settings,
        *,
        memory_service=None,
        runtime_policy: MemoryRuntimePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_policy = runtime_policy or settings.memory_runtime_policy
        self.memory_service = memory_service
        self.pipeline = ContextPipelineService(settings)

    async def assemble_route_context(
        self,
        request: RouteRequest,
        *,
        candidate_agent_ids: list[str],
        candidate_agents=None,
        request_id: str,
        host_history: list[JsonDict] | None = None,
        agent_history: list[JsonDict] | None = None,
        recent_results: list[JsonDict] | None = None,
        recent_events: list[JsonDict] | None = None,
        evidence: list[JsonDict] | None = None,
        active_plan: Plan | JsonDict | None = None,
        intent_hint: str | None = None,
        assembly_session: ContextAssemblySession | None = None,
    ):
        legacy_context = self.build_route_context(
            request,
            candidate_agent_ids=candidate_agent_ids,
            request_id=request_id,
            host_history=host_history,
            agent_history=agent_history,
            recent_results=recent_results,
            recent_events=recent_events,
            evidence=evidence,
            active_plan=active_plan,
            intent_hint=intent_hint,
        )
        if (
            self.settings.context_pipeline_mode == "legacy"
            and not self.runtime_policy.effective_governed_context_memory_enabled
        ):
            return legacy_context, None, assembly_session

        session = assembly_session or ContextAssemblySession(
            assembly_id=f"assembly_{uuid4().hex}",
            request_id=request_id,
            user_id=request.user.id,
            tenant_id=request.user.tenant_id,
        )
        sources = {
            "host_history": host_history or [],
            "agent_history": agent_history or [],
            "recent_results": recent_results or [],
            "recent_events": recent_events or [],
            "evidence": evidence or [],
            "active_plan": active_plan,
        }
        providers = [
            CurrentInputProvider(),
            CurrentAgentProvider(),
            PlanProvider(),
            HistoryProvider(),
            EventProvider(),
            ResultProvider(),
            ArtifactProvider(),
            EvidenceContextProvider(),
            FrontendContextProvider(),
        ]
        if self.memory_service is not None:
            providers.append(
                MemoryRetrievalProvider(
                    self.settings,
                    self.memory_service,
                    stage="route",
                    runtime_policy=self.runtime_policy,
                )
            )
        result = await self.pipeline.assemble(
            request=request,
            purpose="route_decision",
            consumer="router",
            providers=providers,
            budget=self.context_budget_for_request(request),
            candidate_agents=candidate_agents or [],
            sources=sources,
            assembly_session=session,
        )
        legacy_input_hash = hashlib.sha256(
            json.dumps(
                {
                    "request": request.model_dump(mode="json"),
                    "context": legacy_context.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        base_metadata = dict(legacy_context.metadata)
        if self.settings.context_pipeline_mode == "enforced":
            for key in (
                "frontend_context",
                "host_history",
                "agent_history",
                "recent_results",
                "recent_events",
                "active_plan",
            ):
                value = base_metadata.pop(key, None)
                if isinstance(value, list):
                    base_metadata[f"{key}_count"] = len(value)
                elif isinstance(value, dict):
                    base_metadata[f"{key}_keys"] = sorted(str(item) for item in value)[:20]
        metadata = {
            **base_metadata,
            "context_pack": self.governed_context_debug(result),
            "context_trace": self.context_trace_debug(result.trace),
            "context_pipeline": {
                "mode": self.settings.context_pipeline_mode,
                "legacy_input_hash": legacy_input_hash,
                "projection_hash": result.projection.projection_hash,
                "policy_version": result.pack.policy_version,
                "budget_version": result.pack.budget_version,
                "projection_version": result.pack.projection_version,
            },
        }
        artifact_refs = []
        for item in result.pack.items:
            if item.source != "artifact" or not item.structured_value:
                continue
            artifact_refs.extend(
                normalize_artifact_refs(
                    [
                        {
                            key: item.structured_value.get(key)
                            for key in ("artifact_id", "type", "uri", "title", "metadata")
                            if item.structured_value.get(key) is not None
                        }
                    ]
                )
            )
        if len(artifact_refs) > 1:
            metadata["artifact_ambiguity"] = {
                "count": len(artifact_refs),
                "artifact_ids": [item.artifact_id for item in artifact_refs[:10]],
            }
        elif len(artifact_refs) == 1:
            metadata["resolved_artifact_id"] = artifact_refs[0].artifact_id
        selected_evidence = [item for item in result.pack.items if item.source == "evidence"]
        route_evidence = [
            {
                "type": (
                    item.structured_value.get("type") if item.structured_value else "evidence"
                ),
                "id": item.metadata.get("evidence_id"),
                "source_id": item.metadata.get("source_id"),
                "content": item.content,
                "score": item.relevance,
                "matched_agent_ids": (
                    item.structured_value.get("matched_agent_ids") if item.structured_value else []
                ),
            }
            for item in selected_evidence
        ]
        context = legacy_context.model_copy(
            update={
                "artifact_refs": artifact_refs,
                "evidence": route_evidence,
                "metadata": metadata,
            }
        )
        record_recall_usage = getattr(self.memory_service, "record_recall_usage", None)
        if callable(record_recall_usage):
            await record_recall_usage(
                _selected_memory_items(result.pack.items),
                user_id=request.user.id,
                tenant_id=request.user.tenant_id,
                consumer="router",
                request_id=request_id,
                session_id=request.session_id,
            )
        return context, result.projection, session

    def build_route_context(
        self,
        request: RouteRequest,
        *,
        candidate_agent_ids: list[str],
        request_id: str | None = None,
        host_history: list[JsonDict] | None = None,
        agent_history: list[JsonDict] | None = None,
        recent_results: list[JsonDict] | None = None,
        recent_events: list[JsonDict] | None = None,
        evidence: list[JsonDict] | None = None,
        active_plan: Plan | JsonDict | None = None,
        intent_hint: str | None = None,
    ) -> RouteContext:
        relation = "new_task"
        current_agent_id = request.current_agent.agent_id if request.current_agent else None
        if request.current_agent:
            if request.source == "agent_chat":
                relation = "continue_current"
            elif request.input.text and current_agent_id:
                relation = "switch_agent"

        host_history = host_history or []
        agent_history = agent_history or []
        recent_results = recent_results or []
        recent_events = recent_events or []
        evidence = evidence or []

        metadata: JsonDict = {
            "source": request.source,
            "session_id": request.session_id,
            "recent_results_count": len(recent_results),
            "recent_events_count": len(recent_events),
        }
        if request.frontend_context:
            metadata["frontend_context"] = request.frontend_context
        if host_history:
            metadata["host_history"] = host_history[
                : self.settings.router_max_host_history_messages
            ]
        if agent_history:
            metadata["agent_history"] = agent_history[
                : self.settings.router_max_agent_history_messages
            ]
        if recent_results:
            metadata["recent_results"] = recent_results[: self.settings.router_max_recent_results]
        if recent_events:
            metadata["recent_events"] = recent_events[: self.settings.router_max_recent_events]
        if active_plan:
            metadata["active_plan"] = _dump_model(active_plan)

        context_pack = self.build_context_pack(
            request,
            request_id=request_id or request.request_id or f"req_{uuid4().hex}",
            host_history=host_history,
            agent_history=agent_history,
            recent_results=recent_results,
            recent_events=recent_events,
            evidence=evidence,
            active_plan=active_plan,
        )
        metadata["context_pack"] = self.context_pack_debug(context_pack)

        return RouteContext(
            relation=relation,
            current_agent_id=current_agent_id,
            candidate_agent_ids=candidate_agent_ids,
            intent_hint=intent_hint,
            evidence=evidence,
            metadata=metadata,
        )

    def build_context_pack(
        self,
        request: RouteRequest,
        *,
        request_id: str,
        host_history: list[JsonDict] | None = None,
        agent_history: list[JsonDict] | None = None,
        recent_results: list[JsonDict] | None = None,
        recent_events: list[JsonDict] | None = None,
        evidence: list[JsonDict] | None = None,
        active_plan: Plan | JsonDict | None = None,
    ) -> ContextPack:
        budget = self.context_budget_for_request(request)
        candidates = self.build_candidate_items(
            request,
            host_history=host_history or [],
            agent_history=agent_history or [],
            recent_results=recent_results or [],
            recent_events=recent_events or [],
            evidence=evidence or [],
            active_plan=active_plan,
        )
        selected = self.select_items(candidates, budget)
        usage = self.context_usage(selected, budget)
        selection = [self.selection_summary(item) for item in selected]
        return ContextPack(
            pack_id=f"ctx_{uuid4().hex}",
            request_id=request_id,
            session_id=request.session_id,
            budget=budget,
            items=selected,
            selection=selection,
            usage=usage,
            metadata={
                "version": "m4",
                "current_input_item_id": "current_input",
            },
            created_at=datetime.now(UTC),
        )

    def context_budget_for_request(self, request: RouteRequest) -> ContextBudget:
        configured_source_budgets = _parse_source_budgets(
            self.settings.context_default_source_budgets
        )
        budget = ContextBudget(
            max_tokens=self.settings.context_default_token_budget,
            source_budgets=configured_source_budgets,
            per_item_token_limit=self.settings.context_per_item_token_limit,
            per_item_char_limit=self.settings.context_per_item_char_limit,
            chars_per_token=self.settings.context_chars_per_token,
            allow_summary_placeholder=self.settings.context_allow_summary_placeholder,
        )
        requested = request.context_budget or _frontend_budget(request.frontend_context)
        if not requested or not self.settings.context_allow_request_budget_override:
            return budget

        max_tokens = min(requested.max_tokens, self.settings.context_max_token_budget)
        source_budgets = {
            key: min(value, self.settings.context_max_token_budget)
            for key, value in {**configured_source_budgets, **requested.source_budgets}.items()
            if value > 0
        }
        per_item_token_limit = _min_optional_positive(
            requested.per_item_token_limit,
            self.settings.context_per_item_token_limit,
        )
        per_item_char_limit = _min_optional_positive(
            requested.per_item_char_limit,
            self.settings.context_per_item_char_limit,
        )
        return ContextBudget(
            max_tokens=max_tokens,
            source_budgets=source_budgets,
            per_item_token_limit=per_item_token_limit,
            per_item_char_limit=per_item_char_limit,
            chars_per_token=requested.chars_per_token or self.settings.context_chars_per_token,
            allow_summary_placeholder=(
                requested.allow_summary_placeholder
                and self.settings.context_allow_summary_placeholder
            ),
        )

    def build_candidate_items(
        self,
        request: RouteRequest,
        *,
        host_history: list[JsonDict],
        agent_history: list[JsonDict],
        recent_results: list[JsonDict],
        recent_events: list[JsonDict],
        evidence: list[JsonDict],
        active_plan: Plan | JsonDict | None = None,
    ) -> list[ContextItem]:
        items = [
            self._item(
                item_id="current_input",
                source="current_input",
                scope="request",
                role="user",
                content=request.input.text,
                priority=100,
                relevance=1.0,
                must_include=True,
                metadata={
                    "source": request.source,
                    "attachments_count": len(request.input.attachments),
                },
            )
        ]

        if request.current_agent:
            items.append(
                self._item(
                    item_id=f"current_agent:{request.current_agent.agent_id}",
                    source="current_agent",
                    scope="agent",
                    content=f"Current agent: {request.current_agent.agent_id}",
                    structured_value=request.current_agent.model_dump(mode="json"),
                    priority=95,
                    relevance=1.0,
                    must_include=True,
                    metadata={
                        "agent_id": request.current_agent.agent_id,
                        "agent_session_id": request.current_agent.agent_session_id,
                        "run_id": request.current_agent.run_id,
                    },
                )
            )

        if active_plan:
            plan_payload = _dump_model(active_plan)
            items.append(
                self._item(
                    item_id=f"current_plan:{plan_payload.get('plan_id') or request.plan_id or 'active'}",
                    source="current_plan",
                    scope="plan",
                    content=_plan_text(plan_payload),
                    structured_value=plan_payload,
                    priority=90,
                    relevance=0.95,
                    must_include=True,
                    metadata={
                        "plan_id": plan_payload.get("plan_id") or request.plan_id,
                        "step_id": request.step_id,
                    },
                )
            )
        elif request.plan_id:
            items.append(
                self._item(
                    item_id=f"current_plan:{request.plan_id}",
                    source="current_plan",
                    scope="plan",
                    content=f"Active plan: {request.plan_id}, step: {request.step_id or '-'}",
                    structured_value={"plan_id": request.plan_id, "step_id": request.step_id},
                    priority=90,
                    relevance=0.9,
                    must_include=True,
                    metadata={"plan_id": request.plan_id, "step_id": request.step_id},
                )
            )

        if request.frontend_context:
            items.append(
                self._item(
                    item_id="frontend_context",
                    source="frontend_context",
                    scope="request",
                    content=_jsonish(request.frontend_context),
                    structured_value=request.frontend_context,
                    priority=65,
                    relevance=0.7,
                )
            )

        for index, message in enumerate(
            host_history[-self.settings.router_max_host_history_messages :]
        ):
            items.append(self._history_item(message, source="host_history", index=index))
        for index, message in enumerate(
            agent_history[-self.settings.router_max_agent_history_messages :]
        ):
            items.append(self._history_item(message, source="agent_history", index=index))
        for index, result in enumerate(recent_results[: self.settings.router_max_recent_results]):
            items.append(self._result_item(result, index))
        for index, event in enumerate(recent_events[: self.settings.router_max_recent_events]):
            items.append(self._event_item(event, index))
        for index, item in enumerate(evidence):
            items.append(self._evidence_item(item, index))

        return items

    def select_items(
        self, candidates: list[ContextItem], budget: ContextBudget
    ) -> list[ContextItem]:
        prepared = [self._with_estimates(item, budget) for item in candidates]
        ordered = sorted(prepared, key=_item_sort_key)
        used_total = 0
        used_by_source: dict[str, int] = {}
        selected_by_id: dict[str, ContextItem] = {}

        for item in ordered:
            item = self._apply_per_item_limit(item, budget)
            source_budget = budget.source_budgets.get(str(item.source))
            source_used = used_by_source.get(str(item.source), 0)
            source_over_budget = (
                source_budget is not None and source_used + item.token_estimate > source_budget
            )
            total_over_budget = used_total + item.token_estimate > budget.max_tokens

            if item.must_include:
                item = item.model_copy(update={"included": True, "status": _included_status(item)})
                used_total += item.token_estimate
                used_by_source[str(item.source)] = source_used + item.token_estimate
            elif source_over_budget:
                item = item.model_copy(
                    update={
                        "included": False,
                        "status": "dropped",
                        "drop_reason": "source_budget_exceeded",
                    }
                )
            elif total_over_budget:
                item = item.model_copy(
                    update={
                        "included": False,
                        "status": "dropped",
                        "drop_reason": "total_budget_exceeded",
                    }
                )
            else:
                item = item.model_copy(update={"included": True, "status": _included_status(item)})
                used_total += item.token_estimate
                used_by_source[str(item.source)] = source_used + item.token_estimate
            selected_by_id[item.item_id] = item

        return [selected_by_id[item.item_id] for item in prepared]

    def context_usage(self, items: list[ContextItem], budget: ContextBudget) -> ContextUsage:
        included = [item for item in items if item.included]
        dropped = [item for item in items if not item.included]
        source_distribution: dict[str, int] = {}
        for item in included:
            source_distribution[str(item.source)] = source_distribution.get(str(item.source), 0) + 1
        drop_reasons: dict[str, int] = {}
        for item in dropped:
            reason = item.drop_reason or "unknown"
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
        return ContextUsage(
            budget_tokens=budget.max_tokens,
            used_tokens=sum(item.token_estimate for item in included),
            usage_source="estimated",
            included_count=len(included),
            dropped_count=len(dropped),
            truncated_count=len([item for item in items if item.truncated]),
            summary_placeholder_count=len([item for item in items if item.summary_placeholder]),
            source_distribution=source_distribution,
            drop_reasons=drop_reasons,
        )

    def selection_summary(self, item: ContextItem) -> ContextSelectionSummary:
        metadata = redact_value(_bounded_metadata(item.metadata))
        return ContextSelectionSummary(
            item_id=item.item_id,
            source=str(item.source),
            scope=str(item.scope),
            role=str(item.role) if item.role else None,
            priority=item.priority,
            relevance=item.relevance,
            token_estimate=item.token_estimate,
            char_count=item.char_count,
            included=item.included,
            status=item.status,
            drop_reason=item.drop_reason,
            truncated=item.truncated,
            summary_placeholder=item.summary_placeholder,
            agent_id=str(item.metadata.get("agent_id")) if item.metadata.get("agent_id") else None,
            agent_session_id=(
                str(item.metadata.get("agent_session_id"))
                if item.metadata.get("agent_session_id")
                else None
            ),
            created_at=item.created_at,
            metadata=metadata,
        )

    def context_pack_debug(self, pack: ContextPack) -> JsonDict:
        return redact_value(
            {
                "pack_id": pack.pack_id,
                "request_id": pack.request_id,
                "session_id": pack.session_id,
                "trace_id": pack.trace_id,
                "purpose": pack.purpose,
                "consumer": pack.consumer,
                "policy_version": pack.policy_version,
                "budget_version": pack.budget_version,
                "projection_version": pack.projection_version,
                "budget": pack.budget.model_dump(mode="json"),
                "usage": pack.usage.model_dump(mode="json"),
                "selection": [item.model_dump(mode="json") for item in pack.selection],
                "items": [self._debug_item(item) for item in pack.items],
                "metadata": pack.metadata,
                "created_at": pack.created_at.isoformat() if pack.created_at else None,
            }
        )

    def governed_context_debug(self, result) -> JsonDict:
        pack = result.pack
        payload = self.context_pack_debug(pack)
        payload["provider_outcomes"] = [
            item.model_dump(mode="json") for item in result.trace.provider_outcomes
        ]
        payload["decisions"] = [item.model_dump(mode="json") for item in result.trace.decisions]
        payload["projection"] = {
            "projection_id": result.projection.projection_id,
            "projection_hash": result.projection.projection_hash,
            "rendered_chars": result.projection.rendered_chars,
            "token_estimate": result.projection.token_estimate,
            "projection_version": result.projection.projection_version,
        }
        return redact_value(payload)

    def context_trace_debug(self, trace) -> JsonDict:
        return redact_value(
            {
                "trace_id": trace.trace_id,
                "pack_id": trace.pack_id,
                "request_id": trace.request_id,
                "session_id": trace.session_id,
                "purpose": trace.purpose,
                "consumer": trace.consumer,
                "provider_outcomes": [
                    item.model_dump(mode="json") for item in trace.provider_outcomes
                ],
                "decisions": [item.model_dump(mode="json") for item in trace.decisions],
                "budget": trace.budget.model_dump(mode="json") if trace.budget else None,
                "policy_version": trace.policy_version,
                "budget_version": trace.budget_version,
                "projection_version": trace.projection_version,
                "projection_hash": trace.projection_hash,
                "projection_summary": trace.projection_summary,
                "errors": trace.errors,
            }
        )

    def context_pack_log_summary(self, pack_data: object) -> JsonDict | None:
        if not isinstance(pack_data, Mapping):
            return None
        usage = pack_data.get("usage")
        budget = pack_data.get("budget")
        selection = pack_data.get("selection")
        if not isinstance(usage, Mapping) or not isinstance(budget, Mapping):
            return None
        safe_selection = []
        if isinstance(selection, list):
            safe_selection = [
                {
                    "item_id": str(item.get("item_id", "")),
                    "source": str(item.get("source", "")),
                    "scope": str(item.get("scope", "")),
                    "role": item.get("role"),
                    "priority": item.get("priority"),
                    "token_estimate": item.get("token_estimate"),
                    "included": item.get("included"),
                    "status": item.get("status"),
                    "drop_reason": item.get("drop_reason"),
                    "truncated": item.get("truncated"),
                    "summary_placeholder": item.get("summary_placeholder"),
                    "agent_id": item.get("agent_id"),
                    "agent_session_id": item.get("agent_session_id"),
                }
                for item in selection
                if isinstance(item, Mapping)
            ]
        return redact_value(
            {
                "pack_id": pack_data.get("pack_id"),
                "request_id": pack_data.get("request_id"),
                "session_id": pack_data.get("session_id"),
                "trace_id": pack_data.get("trace_id"),
                "purpose": pack_data.get("purpose"),
                "consumer": pack_data.get("consumer"),
                "policy_version": pack_data.get("policy_version"),
                "budget_version": pack_data.get("budget_version"),
                "projection_version": pack_data.get("projection_version"),
                "projection_hash": (
                    pack_data.get("projection", {}).get("projection_hash")
                    if isinstance(pack_data.get("projection"), Mapping)
                    else None
                ),
                "provider_outcomes": [
                    {
                        "provider": item.get("provider"),
                        "status": item.get("status"),
                        "candidate_count": item.get("candidate_count"),
                        "cache_hit": item.get("cache_hit"),
                        "error_code": item.get("error_code"),
                    }
                    for item in pack_data.get("provider_outcomes", [])
                    if isinstance(item, Mapping)
                ],
                "budget": {
                    "max_tokens": budget.get("max_tokens"),
                    "source_budgets": budget.get("source_budgets"),
                    "per_item_token_limit": budget.get("per_item_token_limit"),
                    "per_item_char_limit": budget.get("per_item_char_limit"),
                    "chars_per_token": budget.get("chars_per_token"),
                },
                "usage": {
                    "budget_tokens": usage.get("budget_tokens"),
                    "used_tokens": usage.get("used_tokens"),
                    "usage_source": usage.get("usage_source"),
                    "included_count": usage.get("included_count"),
                    "dropped_count": usage.get("dropped_count"),
                    "truncated_count": usage.get("truncated_count"),
                    "summary_placeholder_count": usage.get("summary_placeholder_count"),
                    "source_distribution": usage.get("source_distribution"),
                    "drop_reasons": usage.get("drop_reasons"),
                },
                "selection": safe_selection,
            }
        )

    def _item(
        self,
        *,
        item_id: str,
        source: str,
        scope: str,
        content: str,
        role: str | None = None,
        structured_value: JsonDict | None = None,
        priority: int = 50,
        relevance: float = 0.5,
        created_at: datetime | None = None,
        must_include: bool = False,
        metadata: JsonDict | None = None,
    ) -> ContextItem:
        return ContextItem(
            item_id=item_id,
            source=source,
            scope=scope,
            role=role,
            content=content,
            structured_value=structured_value,
            priority=priority,
            relevance=relevance,
            created_at=created_at,
            must_include=must_include,
            metadata=metadata or {},
        )

    def _history_item(self, message: JsonDict, *, source: str, index: int) -> ContextItem:
        message_id = _string(message.get("message_id")) or f"{source}:{index}"
        role = _string(message.get("role"))
        content = _string(message.get("content"))
        agent_id = _string(message.get("agent_id")) or None
        created_at = _parse_datetime(message.get("created_at"))
        priority = 48 if source == "agent_history" else 40
        return self._item(
            item_id=f"{source}:{message_id}",
            source=source,
            scope="agent" if source == "agent_history" else "session",
            role=role or None,
            content=content,
            priority=priority,
            relevance=0.6,
            created_at=created_at,
            metadata={
                "message_id": message_id,
                "source": message.get("source"),
                "agent_id": agent_id,
                "agent_session_id": message.get("agent_session_id"),
                "request_id": message.get("request_id"),
                "event_id": message.get("event_id"),
                "metadata": message.get("metadata")
                if isinstance(message.get("metadata"), dict)
                else {},
            },
        )

    def _result_item(self, result: JsonDict, index: int) -> ContextItem:
        result_id = _string(result.get("result_id")) or f"result:{index}"
        output = result.get("output")
        content = _jsonish(output if output is not None else result)
        return self._item(
            item_id=f"recent_result:{result_id}",
            source="recent_result",
            scope="session",
            content=content,
            structured_value=result,
            priority=72,
            relevance=0.75,
            created_at=_parse_datetime(result.get("created_at")),
            metadata={
                "result_id": result_id,
                "run_id": result.get("run_id"),
                "agent_id": result.get("agent_id"),
                "artifact_refs": result.get("artifact_refs") or [],
                "status": result.get("status"),
            },
        )

    def _event_item(self, event: JsonDict, index: int) -> ContextItem:
        event_id = _string(event.get("event_id")) or f"event:{index}"
        return self._item(
            item_id=f"recent_event:{event_id}",
            source="recent_event",
            scope="session",
            content=_jsonish(event.get("payload") if event.get("payload") is not None else event),
            structured_value=event,
            priority=42,
            relevance=0.5,
            created_at=_parse_datetime(event.get("created_at")),
            metadata={
                "event_id": event_id,
                "event_type": event.get("event_type"),
                "agent_id": event.get("agent_id"),
            },
        )

    def _evidence_item(self, evidence: JsonDict, index: int) -> ContextItem:
        evidence_id = (
            _string(evidence.get("id") or evidence.get("evidence_id")) or f"evidence:{index}"
        )
        score = evidence.get("score") or evidence.get("confidence") or evidence.get("relevance")
        relevance = _bounded_float(score, default=0.65)
        return self._item(
            item_id=f"evidence:{evidence_id}",
            source="evidence",
            scope="request",
            content=_evidence_content(evidence),
            structured_value=evidence,
            priority=58,
            relevance=relevance,
            metadata={
                "evidence_id": evidence_id,
                "type": evidence.get("type"),
                "score": score,
                "matched_agent_ids": evidence.get("matched_agent_ids") or [],
                "source_id": evidence.get("source_id"),
            },
        )

    def _with_estimates(self, item: ContextItem, budget: ContextBudget) -> ContextItem:
        char_count = char_count_for_text(item.content)
        token_estimate = token_estimate_for_text(item.content, budget.chars_per_token)
        return item.model_copy(update={"char_count": char_count, "token_estimate": token_estimate})

    def _apply_per_item_limit(self, item: ContextItem, budget: ContextBudget) -> ContextItem:
        content = item.content
        updates: JsonDict = {}
        limit_reason: str | None = None
        if budget.per_item_char_limit and len(content) > budget.per_item_char_limit:
            content = content[: budget.per_item_char_limit].rstrip()
            limit_reason = "per_item_char_limit"
        char_count = char_count_for_text(content)
        token_estimate = token_estimate_for_text(content, budget.chars_per_token)
        if budget.per_item_token_limit and token_estimate > budget.per_item_token_limit:
            max_chars = max(1, int(budget.per_item_token_limit * budget.chars_per_token))
            content = content[:max_chars].rstrip()
            char_count = char_count_for_text(content)
            token_estimate = token_estimate_for_text(content, budget.chars_per_token)
            limit_reason = "per_item_token_limit"
        if limit_reason:
            content = content + "\n[context truncated]"
            char_count = char_count_for_text(content)
            token_estimate = token_estimate_for_text(content, budget.chars_per_token)
            updates.update(
                {
                    "content": content,
                    "char_count": char_count,
                    "token_estimate": token_estimate,
                    "status": "summary_placeholder"
                    if budget.allow_summary_placeholder
                    else "truncated",
                    "truncated": True,
                    "summary_placeholder": budget.allow_summary_placeholder,
                    "drop_reason": limit_reason,
                    "metadata": {
                        **item.metadata,
                        "truncation_reason": limit_reason,
                        "original_char_count": item.char_count,
                        "summary_placeholder": budget.allow_summary_placeholder,
                    },
                }
            )
        return item.model_copy(update=updates) if updates else item

    def _debug_item(self, item: ContextItem) -> JsonDict:
        payload = item.model_dump(mode="json")
        content = str(payload.get("content") or "")
        payload["content"] = content[:500] + ("..." if len(content) > 500 else "")
        payload["metadata"] = _bounded_metadata(
            payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        )
        return payload


def char_count_for_text(value: str) -> int:
    return len(value or "")


def token_estimate_for_text(value: str, chars_per_token: float) -> int:
    if not value:
        return 0
    return max(1, ceil(len(value) / max(chars_per_token, 0.1)))


def _item_sort_key(item: ContextItem) -> tuple[int, float, float, int, str]:
    created = item.created_at.timestamp() if item.created_at else 0
    source_rank = {
        "current_input": 0,
        "current_agent": 1,
        "current_plan": 2,
        "recent_result": 3,
        "evidence": 4,
        "memory": 5,
        "agent_history": 6,
        "host_history": 7,
        "recent_event": 8,
        "frontend_context": 9,
    }.get(str(item.source), 20)
    return (-item.priority, -item.relevance, -created, source_rank, item.item_id)


def _included_status(item: ContextItem) -> str:
    return item.status if item.status != "dropped" else "included"


def _frontend_budget(frontend_context: JsonDict) -> ContextBudget | None:
    raw = frontend_context.get("context_budget")
    if not isinstance(raw, Mapping):
        raw = frontend_context.get("contextBudget")
    if not isinstance(raw, Mapping):
        character_limit = frontend_context.get("context_char_limit") or frontend_context.get(
            "contextCharLimit"
        )
        if isinstance(character_limit, int) and character_limit > 0:
            chars_per_token = float(
                frontend_context.get("chars_per_token")
                or frontend_context.get("charsPerToken")
                or 4.0
            )
            return ContextBudget(max_tokens=max(1, ceil(character_limit / chars_per_token)))
        return None
    data = dict(raw)
    if "max_chars" in data and "max_tokens" not in data:
        max_chars = data.get("max_chars")
        chars_per_token = float(data.get("chars_per_token") or 4.0)
        if isinstance(max_chars, int) and max_chars > 0:
            data["max_tokens"] = max(1, ceil(max_chars / chars_per_token))
    data.pop("max_chars", None)
    if "maxTokens" in data and "max_tokens" not in data:
        data["max_tokens"] = data["maxTokens"]
    if "sourceBudgets" in data and "source_budgets" not in data:
        data["source_budgets"] = data["sourceBudgets"]
    if "perItemTokenLimit" in data and "per_item_token_limit" not in data:
        data["per_item_token_limit"] = data["perItemTokenLimit"]
    if "perItemCharLimit" in data and "per_item_char_limit" not in data:
        data["per_item_char_limit"] = data["perItemCharLimit"]
    if "charsPerToken" in data and "chars_per_token" not in data:
        data["chars_per_token"] = data["charsPerToken"]
    return ContextBudget.model_validate(data)


def _parse_source_budgets(raw: str) -> dict[str, int]:
    budgets: dict[str, int] = {}
    for chunk in raw.split(","):
        if ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        try:
            parsed = int(value.strip())
        except ValueError:
            continue
        if key.strip() and parsed > 0:
            budgets[key.strip()] = parsed
    return budgets


def _min_optional_positive(requested: int | None, configured: int | None) -> int | None:
    values = [value for value in [requested, configured] if value is not None and value > 0]
    return min(values) if values else None


def _selected_memory_items(items: list[ContextItem]) -> list[MemoryContextItem]:
    selected = []
    for item in items:
        if item.source != "memory":
            continue
        value = item.structured_value or {}
        selected.append(
            MemoryContextItem(
                memory_id=str(
                    value.get("memory_id") or item.metadata.get("memory_id") or item.item_id
                ),
                scope=str(value.get("scope") or item.metadata.get("scope") or "stable_fact"),
                content=item.content,
                relevance=item.relevance,
                current_revision_id=value.get("current_revision_id"),
            )
        )
    return selected


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _dump_model(value: object) -> JsonDict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return, union-attr]
    return dict(value) if isinstance(value, Mapping) else {}


def _plan_text(plan: JsonDict) -> str:
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    pending = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        status = str(step.get("status") or "pending")
        if status in {"pending", "running", "blocked"}:
            pending.append(f"{step.get('step_id')}: {step.get('description')}")
    if pending:
        return "Active plan unfinished steps:\n" + "\n".join(pending)
    return _jsonish(plan)


def _jsonish(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _string(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _bounded_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, parsed))


def _evidence_content(evidence: JsonDict) -> str:
    for key in ("content", "text", "snippet", "question"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return _jsonish(evidence)


def _bounded_metadata(metadata: JsonDict) -> JsonDict:
    safe: JsonDict = {}
    for key, value in metadata.items():
        if isinstance(value, str):
            safe[key] = value[:200] + ("..." if len(value) > 200 else "")
        elif isinstance(value, int | float | bool) or value is None:
            safe[key] = value
        elif isinstance(value, list):
            safe[key] = value[:10]
        elif isinstance(value, dict):
            safe[key] = _bounded_metadata(value)
        else:
            safe[key] = str(value)[:200]
    return safe
