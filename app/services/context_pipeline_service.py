import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from math import ceil
from uuid import uuid4

from app.core.config import Settings
from app.core.redaction import SENSITIVE_KEYS, redact_value
from app.schemas.agents import CandidateAgent
from app.schemas.common import JsonDict
from app.schemas.context import (
    ContextBudget,
    ContextCandidate,
    ContextItem,
    ContextPack,
    ContextPipelineResult,
    ContextProjection,
    ContextSelectionSummary,
    ContextTrace,
    ContextTraceDecision,
    ContextUsage,
    ProviderOutcome,
)
from app.schemas.routing import RouteRequest
from app.services.context_providers import (
    ContextProvider,
    ContextProviderContext,
    ProviderCollection,
)


class ContextBudgetExhausted(Exception):
    code = "context_budget_exhausted"

    def __init__(self, message: str = "Critical context cannot fit within the hard budget") -> None:
        super().__init__(message)


class ContextPipelineService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def assemble(
        self,
        *,
        request: RouteRequest,
        purpose: str,
        consumer: str,
        providers: list[ContextProvider],
        budget: ContextBudget | None = None,
        candidate_agents: list[CandidateAgent] | None = None,
        agent=None,
        sources: JsonDict | None = None,
        assembly_session=None,
    ) -> ContextPipelineResult:
        provider_results = await asyncio.gather(
            *[
                self._collect_provider(
                    provider,
                    request=request,
                    purpose=purpose,
                    consumer=consumer,
                    candidate_agents=candidate_agents or [],
                    agent=agent,
                    sources=sources or {},
                    assembly_session=assembly_session,
                )
                for provider in providers
            ]
        )
        candidates = [candidate for items, _ in provider_results for candidate in items]
        outcomes = [outcome for _, outcome in provider_results]
        return await self.assemble_candidates(
            request=request,
            purpose=purpose,
            consumer=consumer,
            candidates=candidates,
            budget=budget,
            candidate_agents=candidate_agents,
            provider_outcomes=outcomes,
        )

    async def _collect_provider(
        self,
        provider: ContextProvider,
        *,
        request: RouteRequest,
        purpose: str,
        consumer: str,
        candidate_agents: list[CandidateAgent],
        agent,
        sources: JsonDict,
        assembly_session,
    ) -> tuple[list[ContextCandidate], ProviderOutcome]:
        context = ContextProviderContext(
            request=request.model_copy(deep=True),
            purpose=purpose,
            consumer=consumer,
            candidate_agents=list(candidate_agents),
            agent=agent,
            sources=dict(sources),
        )
        if not provider.applies(context):
            return [], ProviderOutcome(provider=provider.name, status="skipped")
        cache_key = provider.cache_key(context)
        can_use_cache = _assembly_matches(assembly_session, request)
        if can_use_cache and cache_key in assembly_session.provider_cache:
            cached = assembly_session.provider_cache[cache_key]
            candidates = [ContextCandidate.model_validate(item) for item in cached]
            bind_cached = getattr(provider, "bind_cached", None)
            if callable(bind_cached):
                candidates = bind_cached(candidates, context)
            return candidates, ProviderOutcome(
                provider=provider.name,
                status="ok" if candidates else "empty",
                candidate_count=len(candidates),
                cache_hit=True,
            )
        started = time.perf_counter()
        try:
            operation = provider.collect(context)
            if provider.timeout_seconds is not None:
                collected = await asyncio.wait_for(operation, timeout=provider.timeout_seconds)
            else:
                collected = await operation
            if isinstance(collected, ProviderCollection):
                candidates = collected.candidates
                status = collected.status
                error_code = collected.error_code
                outcome_metadata = collected.metadata
            else:
                candidates = collected
                status = "ok" if candidates else "empty"
                error_code = None
                outcome_metadata = {}
            candidates = [ContextCandidate.model_validate(item) for item in candidates]
            if can_use_cache and getattr(provider, "cacheable", True):
                assembly_session.provider_cache[cache_key] = [
                    item.model_dump(mode="json") for item in candidates
                ]
            return candidates, ProviderOutcome(
                provider=provider.name,
                status=status,
                candidate_count=len(candidates),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error_code=error_code,
                metadata=outcome_metadata,
            )
        except TimeoutError:
            return [], ProviderOutcome(
                provider=provider.name,
                status="timeout",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error_code="provider_timeout",
            )
        except Exception as exc:
            return [], ProviderOutcome(
                provider=provider.name,
                status="error",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error_code=type(exc).__name__,
                metadata={"message": str(exc)[:200]},
            )

    async def assemble_candidates(
        self,
        *,
        request: RouteRequest,
        purpose: str,
        consumer: str,
        candidates: list[ContextCandidate],
        budget: ContextBudget | None = None,
        candidate_agents: list[CandidateAgent] | None = None,
        provider_outcomes: list[ProviderOutcome] | None = None,
    ) -> ContextPipelineResult:
        request_id = request.request_id or f"req_{uuid4().hex}"
        trace_id = f"trace_{uuid4().hex}"
        pack_id = f"ctx_{uuid4().hex}"
        effective_budget = self._effective_budget(
            budget or self._default_budget(), candidate_agents or []
        )
        governed, decisions = self._govern(
            candidates,
            purpose=purpose,
            consumer=consumer,
        )
        governed, conflict_decisions = self._dedupe_and_resolve_conflicts(
            governed,
            purpose=purpose,
            consumer=consumer,
        )
        decisions.extend(conflict_decisions)
        items, selection, usage, projection_payload, budget_decisions = self._select_and_project(
            request=request,
            purpose=purpose,
            consumer=consumer,
            candidates=governed,
            budget=effective_budget,
        )
        decisions.extend(budget_decisions)
        rendered = _stable_json(projection_payload)
        projection_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        projection = ContextProjection(
            projection_id=f"projection_{uuid4().hex}",
            pack_id=pack_id,
            request_id=request_id,
            session_id=request.session_id,
            purpose=purpose,
            consumer=consumer,
            payload=projection_payload,
            rendered_chars=len(rendered),
            token_estimate=_token_estimate(rendered, effective_budget.chars_per_token),
            projection_hash=projection_hash,
            projection_version=self.settings.context_projection_version,
        )
        pack = ContextPack(
            pack_id=pack_id,
            trace_id=trace_id,
            request_id=request_id,
            session_id=request.session_id,
            purpose=purpose,
            consumer=consumer,
            budget=effective_budget,
            items=items,
            selection=selection,
            usage=usage.model_copy(update={"used_tokens": projection.token_estimate}),
            policy_version=self.settings.context_policy_version,
            budget_version=self.settings.context_budget_version,
            projection_version=self.settings.context_projection_version,
            metadata={"pipeline": "governed", "version": "v1"},
            created_at=datetime.now(UTC),
        )
        trace = ContextTrace(
            trace_id=trace_id,
            pack_id=pack_id,
            request_id=request_id,
            session_id=request.session_id,
            purpose=purpose,
            consumer=consumer,
            provider_outcomes=provider_outcomes or [],
            decisions=decisions,
            budget=pack.usage,
            policy_version=self.settings.context_policy_version,
            budget_version=self.settings.context_budget_version,
            projection_version=self.settings.context_projection_version,
            projection_hash=projection_hash,
            projection_summary={
                "rendered_chars": projection.rendered_chars,
                "token_estimate": projection.token_estimate,
                "included_count": pack.usage.included_count,
                "dropped_count": pack.usage.dropped_count,
            },
            created_at=datetime.now(UTC),
        )
        return ContextPipelineResult(pack=pack, projection=projection, trace=trace)

    def _default_budget(self) -> ContextBudget:
        return ContextBudget(
            max_tokens=self.settings.context_default_token_budget,
            source_budgets=_parse_source_budgets(self.settings.context_default_source_budgets),
            per_item_token_limit=self.settings.context_per_item_token_limit,
            per_item_char_limit=self.settings.context_per_item_char_limit,
            chars_per_token=self.settings.context_chars_per_token,
            allow_summary_placeholder=self.settings.context_allow_summary_placeholder,
        )

    def _effective_budget(
        self,
        requested: ContextBudget,
        candidate_agents: list[CandidateAgent],
    ) -> ContextBudget:
        candidate_tokens = _token_estimate(
            _stable_json([agent.model_dump(mode="json") for agent in candidate_agents]),
            requested.chars_per_token,
        )
        reserved = (
            self.settings.context_system_reserve_tokens
            + self.settings.context_schema_reserve_tokens
            + self.settings.context_rules_reserve_tokens
            + self.settings.context_output_reserve_tokens
            + candidate_tokens
        )
        model_available = max(1, self.settings.context_model_window_tokens - reserved)
        return requested.model_copy(
            update={"max_tokens": min(requested.max_tokens, model_available)}
        )

    def _govern(
        self,
        candidates: list[ContextCandidate],
        *,
        purpose: str,
        consumer: str,
    ) -> tuple[list[ContextCandidate], list[ContextTraceDecision]]:
        now = datetime.now(UTC)
        eligible: list[ContextCandidate] = []
        decisions: list[ContextTraceDecision] = []
        for candidate in candidates:
            outcome = None
            reason = None
            if candidate.purpose != purpose:
                outcome, reason = "purpose_denied", "purpose_mismatch"
            elif not _consumer_allowed(candidate, consumer):
                outcome, reason = "visibility_denied", "consumer_not_allowed"
            elif not candidate.permission_granted:
                outcome, reason = "permission_denied", "permission_denied"
            elif not candidate.enabled or candidate.deleted:
                outcome, reason = "disabled", "source_disabled_or_deleted"
            elif candidate.expires_at is not None and _as_utc(candidate.expires_at) <= now:
                outcome, reason = "expired", "expired"
            if outcome:
                decisions.append(_decision(candidate, outcome=outcome, reason=reason))
                continue
            eligible.append(
                candidate.model_copy(
                    update={
                        "structured_value": _bounded_projection(
                            candidate.structured_value,
                            source=str(candidate.source),
                        ),
                        "metadata": redact_value(_bounded_value(candidate.metadata)),
                    }
                )
            )
        return eligible, decisions

    def _dedupe_and_resolve_conflicts(
        self,
        candidates: list[ContextCandidate],
        *,
        purpose: str,
        consumer: str,
    ) -> tuple[list[ContextCandidate], list[ContextTraceDecision]]:
        ordered = sorted(candidates, key=_candidate_sort_key)
        canonical: list[ContextCandidate] = []
        decisions: list[ContextTraceDecision] = []
        seen_keys: dict[str, ContextCandidate] = {}
        seen_content: dict[tuple[str, str], ContextCandidate] = {}
        for candidate in ordered:
            stable_key = candidate.dedupe_key or candidate.source_ref
            content_key = _normalized_content_hash(candidate.content)
            content_family = _content_dedupe_family(str(candidate.source))
            duplicate = seen_keys.get(stable_key) if stable_key else None
            duplicate = duplicate or (
                seen_content.get((content_family, content_key))
                if content_family and content_key
                else None
            )
            if duplicate is not None:
                decisions.append(
                    _decision(
                        candidate,
                        outcome="duplicate_dropped",
                        reason="duplicate_candidate",
                        related_refs=[_candidate_ref(duplicate)],
                    )
                )
                continue
            canonical.append(candidate)
            if stable_key:
                seen_keys[stable_key] = candidate
            if content_family and content_key:
                seen_content[(content_family, content_key)] = candidate

        by_conflict: dict[str, list[ContextCandidate]] = {}
        for candidate in canonical:
            if candidate.conflict_key:
                by_conflict.setdefault(candidate.conflict_key, []).append(candidate)
        removed: set[str] = set()
        synthetic: list[ContextCandidate] = []
        for conflict_key, group in by_conflict.items():
            values = {_fact_value(candidate) for candidate in group}
            if len(group) < 2 or len(values) < 2:
                continue
            current = next((item for item in group if item.source == "current_input"), None)
            memories = [item for item in group if item.source == "memory"]
            if current and memories:
                for memory in memories:
                    removed.add(memory.candidate_id)
                    decisions.append(
                        _decision(
                            memory,
                            outcome="overridden_for_turn",
                            reason="current_input_override",
                            related_refs=[_candidate_ref(current)],
                        )
                    )
                continue
            winner = sorted(group, key=_candidate_sort_key)[0]
            winner_rank = _authority_rank(winner.authority)
            peers = [item for item in group if _authority_rank(item.authority) == winner_rank]
            high_risk = any(item.metadata.get("risk") == "high" for item in group)
            if high_risk and len({_fact_value(item) for item in peers}) > 1:
                for item in group:
                    removed.add(item.candidate_id)
                    decisions.append(
                        _decision(
                            item,
                            outcome="unresolved_conflict",
                            reason="authoritative_high_risk_conflict",
                            conflict_key=conflict_key,
                            related_refs=[_candidate_ref(peer) for peer in group if peer != item],
                        )
                    )
                synthetic.append(
                    ContextCandidate(
                        candidate_id=f"conflict:{hashlib.sha256(conflict_key.encode()).hexdigest()[:12]}",
                        source="system",
                        scope="request",
                        content="Conflicting authoritative context requires clarification.",
                        purpose=purpose,
                        consumers=[consumer],
                        authority="authoritative",
                        visibility=["router"] if consumer == "router" else ["agent"],
                        priority=98,
                        must_include=True,
                        conflict_key=conflict_key,
                        metadata={"conflict_outcome": "unresolved_conflict"},
                    )
                )
                continue
            for item in group:
                if item.candidate_id == winner.candidate_id:
                    continue
                removed.add(item.candidate_id)
                decisions.append(
                    _decision(
                        item,
                        outcome="superseded",
                        reason="higher_authority_or_fresher_candidate",
                        conflict_key=conflict_key,
                        related_refs=[_candidate_ref(winner)],
                    )
                )
        return [
            item for item in canonical if item.candidate_id not in removed
        ] + synthetic, decisions

    def _select_and_project(
        self,
        *,
        request: RouteRequest,
        purpose: str,
        consumer: str,
        candidates: list[ContextCandidate],
        budget: ContextBudget,
    ) -> tuple[
        list[ContextItem],
        list[ContextSelectionSummary],
        ContextUsage,
        JsonDict,
        list[ContextTraceDecision],
    ]:
        payload: JsonDict = {
            "request": _request_control_projection(request),
            "context": {"items": []},
        }
        if _payload_tokens(payload, budget) > budget.max_tokens:
            raise ContextBudgetExhausted("Projection wrapper exceeds the hard context budget")
        included: list[ContextItem] = []
        selection: list[ContextSelectionSummary] = []
        decisions: list[ContextTraceDecision] = []
        source_used: dict[str, int] = {}
        drop_reasons: dict[str, int] = {}
        truncated_count = 0
        ordered = sorted(candidates, key=_candidate_sort_key)
        for candidate in ordered:
            rendered, item = _render_candidate(candidate, purpose, consumer, budget)
            candidate_tokens = _token_estimate(_stable_json(rendered), budget.chars_per_token)
            source_budget = budget.source_budgets.get(str(candidate.source))
            source_over = bool(
                source_budget is not None
                and source_used.get(str(candidate.source), 0) + candidate_tokens > source_budget
            )
            trial_payload = _with_projected_item(payload, rendered)
            fits = not source_over and _payload_tokens(trial_payload, budget) <= budget.max_tokens
            if not fits and candidate.must_include:
                minimal = _minimal_rendered(candidate)
                trial_payload = _with_projected_item(payload, minimal)
                minimal_tokens = _token_estimate(_stable_json(minimal), budget.chars_per_token)
                minimal_source_over = bool(
                    source_budget is not None
                    and source_used.get(str(candidate.source), 0) + minimal_tokens > source_budget
                )
                if (
                    _payload_tokens(trial_payload, budget) > budget.max_tokens
                    or minimal_source_over
                ):
                    raise ContextBudgetExhausted()
                rendered = minimal
                item = item.model_copy(
                    update={
                        "content": "",
                        "structured_value": None,
                        "truncated": True,
                        "summary_placeholder": True,
                        "status": "summary_placeholder",
                        "drop_reason": "hard_budget_minimum_representation",
                    }
                )
                candidate_tokens = minimal_tokens
                fits = True
            if fits:
                payload = trial_payload
                item = item.model_copy(
                    update={
                        "included": True,
                        "token_estimate": candidate_tokens,
                        "char_count": len(_stable_json(rendered)),
                        "status": "summary_placeholder"
                        if item.summary_placeholder
                        else "truncated"
                        if item.truncated
                        else "included",
                    }
                )
                included.append(item)
                source_used[str(candidate.source)] = (
                    source_used.get(str(candidate.source), 0) + candidate_tokens
                )
                truncated_count += int(item.truncated)
                outcome = (
                    "referenced"
                    if item.summary_placeholder
                    else "truncated"
                    if item.truncated
                    else "included"
                )
                decisions.append(
                    _decision(candidate, outcome=outcome, token_estimate=candidate_tokens)
                )
                selection.append(_selection_summary(item))
                continue
            reason = "source_budget_exceeded" if source_over else "total_budget_exceeded"
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
            dropped_item = item.model_copy(
                update={
                    "included": False,
                    "status": "dropped",
                    "drop_reason": reason,
                    "token_estimate": candidate_tokens,
                    "char_count": len(_stable_json(rendered)),
                }
            )
            selection.append(_selection_summary(dropped_item))
            decisions.append(
                _decision(
                    candidate,
                    outcome="budget_dropped",
                    reason=reason,
                    token_estimate=candidate_tokens,
                )
            )
        usage = ContextUsage(
            budget_tokens=budget.max_tokens,
            used_tokens=_payload_tokens(payload, budget),
            usage_source="estimated",
            included_count=len(included),
            dropped_count=len(selection) - len(included),
            truncated_count=truncated_count,
            summary_placeholder_count=len([item for item in included if item.summary_placeholder]),
            source_distribution={
                source: count for source, count in _source_counts(included).items()
            },
            drop_reasons=drop_reasons,
        )
        return included, selection, usage, payload, decisions


def _render_candidate(
    candidate: ContextCandidate,
    purpose: str,
    consumer: str,
    budget: ContextBudget,
) -> tuple[JsonDict, ContextItem]:
    content = candidate.content
    truncated = False
    reason = None
    if budget.per_item_char_limit and len(content) > budget.per_item_char_limit:
        content = content[: budget.per_item_char_limit].rstrip()
        truncated = True
        reason = "per_item_char_limit"
    if budget.per_item_token_limit:
        limit_chars = max(1, int(budget.per_item_token_limit * budget.chars_per_token))
        if len(content) > limit_chars:
            content = content[:limit_chars].rstrip()
            truncated = True
            reason = "per_item_token_limit"
    if truncated and budget.allow_summary_placeholder:
        content = f"{content}\n[context truncated]"
    rendered: JsonDict = {
        "item_id": candidate.candidate_id,
        "source": str(candidate.source),
        "scope": str(candidate.scope),
        "authority": candidate.authority,
    }
    if candidate.role:
        rendered["role"] = str(candidate.role)
    if content:
        rendered["content"] = content
    if candidate.structured_value:
        rendered["structured_value"] = candidate.structured_value
    if candidate.source_ref:
        rendered["source_ref"] = candidate.source_ref
    item = ContextItem(
        item_id=candidate.candidate_id,
        source=candidate.source,
        scope=candidate.scope,
        role=candidate.role,
        content=content,
        structured_value=candidate.structured_value,
        priority=candidate.priority,
        relevance=candidate.relevance,
        created_at=candidate.created_at,
        truncated=truncated,
        summary_placeholder=truncated and budget.allow_summary_placeholder,
        must_include=candidate.must_include,
        purpose=purpose,
        consumer=consumer,
        authority=candidate.authority,
        visibility=candidate.visibility,
        source_ref=candidate.source_ref,
        dedupe_key=candidate.dedupe_key,
        conflict_key=candidate.conflict_key,
        drop_reason=reason,
        metadata=candidate.metadata,
    )
    return rendered, item


def _bounded_projection(value: JsonDict | None, *, source: str) -> JsonDict | None:
    if not value:
        return None
    allowed = {
        "current_agent": {"agent_id", "run_id", "agent_session_id"},
        "current_plan": {
            "plan_id",
            "session_id",
            "status",
            "current_step_id",
            "execution_policy",
            "steps",
        },
        "recent_result": {
            "result_id",
            "run_id",
            "agent_id",
            "plan_id",
            "step_id",
            "status",
            "artifact_refs",
            "summary",
        },
        "recent_event": {
            "event_id",
            "run_id",
            "request_id",
            "agent_id",
            "event_type",
            "status",
            "plan_id",
            "step_id",
            "payload",
        },
        "artifact": {"artifact_id", "type", "uri", "title", "result_id"},
        "evidence": {
            "id",
            "evidence_id",
            "type",
            "source_id",
            "score",
            "confidence",
            "title",
            "uri",
            "matched_agent_ids",
        },
        "memory": {
            "memory_id",
            "scope",
            "subject_type",
            "subject_id",
            "confidence",
            "importance",
            "source",
            "ttl_expires_at",
        },
        "knowledge": {
            "item_id",
            "source_id",
            "score",
            "title",
            "uri",
            "citation",
        },
        "frontend_context": {
            "page",
            "route",
            "selection",
            "locale",
            "timezone",
            "interaction",
            "ui_state",
        },
    }.get(source, set())
    if not allowed:
        return None
    projected = {key: value[key] for key in allowed if key in value}
    return redact_value(_bounded_value(projected))


def _bounded_value(value: object, *, depth: int = 0) -> object:
    if depth >= 4:
        return "[bounded]"
    if isinstance(value, dict):
        result: JsonDict = {}
        for key, item in list(value.items())[:20]:
            if str(key).lower() in SENSITIVE_KEYS:
                result[str(key)] = "***REDACTED***"
            else:
                result[str(key)] = _bounded_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_bounded_value(item, depth=depth + 1) for item in value[:10]]
    if isinstance(value, str):
        return value[:500] + ("..." if len(value) > 500 else "")
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)[:500]


def _request_control_projection(request: RouteRequest) -> JsonDict:
    return {
        "request_id": request.request_id,
        "session_id": request.session_id,
        "source": request.source,
        "event_id": request.event_id,
        "plan_id": request.plan_id,
        "step_id": request.step_id,
        "user": {
            "id": request.user.id,
            "roles": request.user.roles,
            "groups": request.user.groups,
            "tenant_id": request.user.tenant_id,
        },
    }


def _consumer_allowed(candidate: ContextCandidate, consumer: str) -> bool:
    if consumer in candidate.consumers or "*" in candidate.consumers:
        allowed = True
    elif consumer.startswith("agent:") and "agent:*" in candidate.consumers:
        allowed = True
    else:
        allowed = False
    if not allowed:
        return False
    if consumer == "router" and candidate.visibility and "router" not in candidate.visibility:
        return False
    if consumer.startswith("agent:"):
        agent_id = consumer.split(":", 1)[1]
        if candidate.visibility and "agent" not in candidate.visibility:
            return False
        if candidate.allowed_agent_ids and agent_id not in candidate.allowed_agent_ids:
            return False
    return True


def _candidate_sort_key(candidate: ContextCandidate) -> tuple[int, int, float, float, str]:
    created = candidate.created_at.timestamp() if candidate.created_at else 0
    return (
        -int(candidate.must_include),
        _authority_rank(candidate.authority),
        -candidate.priority,
        -max(candidate.relevance, created / 10**10),
        candidate.candidate_id,
    )


def _authority_rank(authority: str) -> int:
    return {
        "authoritative": 0,
        "derived": 1,
        "host_asserted": 2,
        "model_generated": 3,
    }.get(authority, 10)


def _fact_value(candidate: ContextCandidate) -> str:
    value = candidate.fact_value if candidate.fact_value is not None else candidate.content
    return str(value).strip().lower()


def _candidate_ref(candidate: ContextCandidate) -> str:
    return candidate.source_ref or candidate.candidate_id


def _normalized_content_hash(content: str) -> str | None:
    normalized = " ".join(content.lower().split())
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _content_dedupe_family(source: str) -> str | None:
    if source in {"evidence", "knowledge"}:
        return "retrieval_evidence"
    if source in {"current_input", "memory"}:
        return "current_user_fact"
    if source in {"host_history", "agent_history"}:
        return "history"
    return None


def _decision(
    candidate: ContextCandidate,
    *,
    outcome: str,
    reason: str | None = None,
    conflict_key: str | None = None,
    related_refs: list[str] | None = None,
    token_estimate: int = 0,
) -> ContextTraceDecision:
    return ContextTraceDecision(
        candidate_id=candidate.candidate_id,
        source=str(candidate.source),
        source_ref=candidate.source_ref,
        authority=candidate.authority,
        outcome=outcome,
        reason=reason,
        conflict_key=conflict_key or candidate.conflict_key,
        related_refs=related_refs or [],
        token_estimate=token_estimate,
    )


def _minimal_rendered(candidate: ContextCandidate) -> JsonDict:
    return {
        "item_id": candidate.candidate_id,
        "source": str(candidate.source),
        "source_ref": candidate.source_ref or candidate.candidate_id,
    }


def _selection_summary(item: ContextItem) -> ContextSelectionSummary:
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
        metadata=redact_value(_bounded_value(item.metadata)),
    )


def _with_projected_item(payload: JsonDict, item: JsonDict) -> JsonDict:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    items = context.get("items") if isinstance(context.get("items"), list) else []
    return {**payload, "context": {**context, "items": [*items, item]}}


def _payload_tokens(payload: JsonDict, budget: ContextBudget) -> int:
    return _token_estimate(_stable_json(payload), budget.chars_per_token)


def _token_estimate(value: str, chars_per_token: float) -> int:
    if not value:
        return 0
    return max(1, ceil(len(value) / max(chars_per_token, 0.1)))


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _source_counts(items: list[ContextItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.source)] = counts.get(str(item.source), 0) + 1
    return counts


def _parse_source_budgets(raw: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in raw.split(","):
        key, separator, value = item.partition(":")
        if not separator:
            continue
        try:
            parsed = int(value.strip())
        except ValueError:
            continue
        if key.strip() and parsed > 0:
            result[key.strip()] = parsed
    return result


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _assembly_matches(assembly_session, request: RouteRequest) -> bool:
    if assembly_session is None:
        return False
    if assembly_session.user_id != request.user.id:
        return False
    if assembly_session.tenant_id != request.user.tenant_id:
        return False
    request_id = request.request_id or assembly_session.request_id
    return assembly_session.request_id == request_id
