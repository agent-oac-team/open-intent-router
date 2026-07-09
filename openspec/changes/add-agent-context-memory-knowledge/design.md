## Context

The current router already supports Agent Registry filtering, fixed-question Evidence Provider behavior, Context Pack budgeting, Plan execution policy, and basic invocation. M5 and M6 extend this into a broader platform context layer: the central router and downstream Agents need to perceive user memory and knowledge evidence consistently, but Agents should not freely decide when or how to call retrieval systems.

The important product boundary is that memory and knowledge are governed platform context, not arbitrary Agent tools. Agent Definitions declare what they need. The router, Invoker, or a fixed workflow node performs retrieval according to that declaration, then passes deterministic `memory_context` and `knowledge_context` fields to the target Agent.

This change keeps one OpenSpec change rather than splitting M5 and M6 because the implementation hinge is shared: `AgentDefinition.context`, prefetch orchestration, Context Pack integration, and invocation input assembly. Adapter-specific work such as RAG-Anything can be proposed later.

## Goals / Non-Goals

**Goals:**

- Add `AgentDefinition.context` declarations for memory and knowledge.
- Automatically prefetch memory every time an Agent route/invocation uses configured memory context.
- Keep knowledge prefetch disabled by default and enable it only when the Agent explicitly declares it.
- Pass structured `memory_context` and `knowledge_context` fields with `items` plus `summary`; knowledge also carries citations.
- Use mem0 as the default memory strategy engine for extraction, semantic recall, update, and merge behavior.
- Keep open-intent-router responsible for tenant enablement, scopes, TTL, visibility, permissions, redaction, audit, and Agent-level context eligibility.
- Support two retrieval execution modes:
  - `prefetch`: router or Invoker retrieves before invoking the Agent.
  - `controlled_retrieval`: a fixed workflow node calls memory recall or `/knowledge/search` through a predefined template.
- Make `/knowledge/search` a general governed retrieval API, not a hard `agent_id`-bound endpoint.
- Preserve fixed questions as M6 Evidence Provider behavior for question-to-intent mapping only.
- Use PostgreSQL for metadata/logs and Milvus for memory and knowledge vectors as the standard development infrastructure.

**Non-Goals:**

- No unrestricted Agent self-retrieval or model-selected memory/knowledge tool use.
- No fixed FAQ answer feature.
- No RAG-Anything adapter in this change.
- No complete user-facing memory notification surface in normal chat; memory writes are visible in debug/admin views.
- No redesign of existing Agent access policy semantics.
- No replacement of M4 Context Pack budgeting; M5/M6 feed Context Pack rather than bypassing it.

## Decisions

### Decision 1: Agent context configuration is the product entry point

Agent Definition gains a `context` section. It declares which memory scopes and knowledge sources the platform may retrieve for that Agent, how retrieval runs, and how much context can be passed.

Example shape:

```yaml
context:
  memory:
    mode: prefetch
    scopes:
      - user_preference
      - stable_fact
      - task_memory
    max_items: 5
  knowledge:
    mode: disabled
    source_ids:
      - product_docs
    max_items: 5
```

The router and Invoker treat this declaration as a policy input. The target Agent receives only stable fields such as `memory_context` and `knowledge_context`; it does not need to know whether the source was mem0, Milvus, a fixed question, or a future adapter.

Alternative considered: expose memory and knowledge APIs directly to every Agent. This is more flexible, but it lets Agent prompts and platform-specific bots decide retrieval behavior freely, which weakens auditability and makes low-code Agents harder to govern.

### Decision 2: Memory prefetch runs for configured Agents on every route/invocation

When an Agent has `context.memory.mode=prefetch`, the router or Invoker recalls memory before invoking that Agent. The output is a structured `memory_context`:

```json
{
  "summary": "...",
  "items": [],
  "truncated": false,
  "status": "ok"
}
```

First supported memory scopes are:

- `user_preference`
- `stable_fact`
- `task_memory`
- `artifact_reference`
- `session_summary`

Current user input controls the current turn but MUST NOT directly rewrite long-term memory. When current input conflicts with recalled memory, the system records a session override or memory conflict signal. Long-term updates require mem0 extraction plus OIR policy approval, confirmation policy, or a high-confidence low-risk rule.

Alternative considered: make memory recall optional per request only. That would reduce latency, but it does not satisfy the goal that users perceive memory across the whole system.

### Decision 3: mem0 owns memory strategy, OIR owns governance

mem0 is the default strategy layer for memory extraction, semantic search, merge, and update behavior. OIR wraps it through a memory adapter and adds required platform governance:

- tenant and host memory enablement
- Agent context scopes
- visibility and subject isolation
- TTL and cleanup
- permission checks
- redaction
- recall/write logs
- Context Pack budgets
- debug/admin visibility

Task memory, session summaries, and artifact references default to 14-day cleanup. User preferences and stable facts are long-lived by default, remain user-manageable, and can be overridden by tenant policy.

Alternative considered: implement all memory extraction and update rules directly in OIR. That gives maximum control, but wastes mature memory behavior available in mem0 and delays the M5 platform contract.

### Decision 4: Knowledge prefetch is explicit, and `/knowledge/search` is general

Knowledge context is disabled unless the Agent declares it. If enabled with `mode=prefetch`, the router or Invoker retrieves before invocation. If enabled with `mode=controlled_retrieval`, a fixed workflow node calls retrieval through a configured query template.

`/knowledge/search` is a general governed API. It accepts caller metadata rather than requiring `agent_id` as the binding key:

- `caller_type`: `router`, `agent`, `host`, or `admin`
- `caller_id`: optional identifier such as Agent ID or workflow node ID
- `purpose`: `route_evidence`, `agent_execution`, `debug`, or `preview`
- `source_ids` or source filters
- user, tenant, and subject context required by policy

Knowledge source permissions live on the knowledge source and policy layer. Agent Definition declares desired sources, but KnowledgeService performs final access decisions.

Alternative considered: bind knowledge retrieval directly to Agent ID. This is simple, but it makes route evidence, host preview, admin debug, and workflow-node retrieval awkward.

### Decision 5: Context fields are structured, with summaries for low-code Agents

The invocation input includes both structure and a compact summary:

```json
{
  "memory_context": {
    "summary": "...",
    "items": [],
    "truncated": false,
    "status": "ok"
  },
  "knowledge_context": {
    "summary": "...",
    "items": [],
    "citations": [],
    "source_ids": [],
    "truncated": false,
    "status": "ok"
  }
}
```

Low-code Bots and HTTP Agents may consume only `summary`. More capable Agents may inspect `items` and citations. Context Pack still controls budget, truncation, and debug summaries.

Alternative considered: pass only a formatted string. That is easy for Bots but loses citations, debugability, budget metadata, and structured testability.

### Decision 6: Retrieval failure degrades, it does not block normal invocation

Prefetch should run concurrently where possible and use bounded timeouts. If memory or knowledge retrieval fails or times out, the invocation continues with `status=timeout` or `status=error` in the corresponding context field, unless a future Agent policy marks that context as required.

Suggested initial timeout ranges:

- memory prefetch: 500 to 800 ms
- knowledge prefetch: 800 to 1500 ms

Alternative considered: make retrieval required by default. This would maximize context quality but makes all Agent calls depend on Milvus, mem0, and knowledge availability.

### Decision 7: Fixed questions remain special Evidence Provider rules

Fixed questions stay under M6 Evidence Provider scheduling. They only map question to intent or route override. They do not provide fixed FAQ answers in this change.

Strong fixed-question matches keep their current semantics:

1. access-filtered candidates define the hard boundary
2. strong fixed question can short-circuit LLM routing when target is allowed
3. denied strong match returns no-permission without LLM fallback
4. weak hints enter context without shrinking LLM candidates

Alternative considered: move fixed questions into the Agent context system. That would blur deterministic route rules with Agent execution context and reopen conflicts already resolved by M3.

### Decision 8: PostgreSQL and Milvus are standard infrastructure, but domains stay separate

The development stack uses PostgreSQL for metadata and logs, and Milvus for vector retrieval. Memory and knowledge share infrastructure but remain logically separate:

- Milvus collections: memory vectors and knowledge vectors are separate collections.
- PostgreSQL tables: memory items/events, knowledge sources/chunks, retrieval logs, and evidence logs are separate tables.

This keeps deployment simple while preserving governance differences: memory is mutable subject state; knowledge is sourced evidence.

Alternative considered: use one shared vector collection and one shared metadata table. That reduces operational setup but makes TTL, permissions, citations, and audit behavior harder to enforce.

## Risks / Trade-offs

- [Risk] Every Agent invocation with memory prefetch adds latency. -> Mitigation: run prefetch concurrently, enforce timeouts, and degrade with structured status.
- [Risk] mem0 default memory behavior may conflict with product governance. -> Mitigation: keep mem0 behind an adapter and apply OIR policy before write, recall exposure, TTL, and Agent visibility.
- [Risk] Knowledge prefetch can inject irrelevant evidence. -> Mitigation: keep knowledge disabled by default and require Agent Definition to opt in with source IDs, limits, and mode.
- [Risk] Context fields can grow beyond target Agent limits. -> Mitigation: pass all recalled content through M4 Context Pack budgets and expose truncation metadata.
- [Risk] Automatic memory writes may be wrong. -> Mitigation: expose write and recall details in debug/admin views, keep high-risk writes pending or rejected by policy, and provide update/delete APIs.
- [Risk] Standard PostgreSQL + Milvus dependencies increase development setup cost. -> Mitigation: keep service boundaries clean so future fallback stores can be added without changing Agent context contracts.

## Migration Plan

1. Extend Agent Definition schema and example config with `context.memory` and `context.knowledge`.
2. Add context field schemas for `memory_context`, `knowledge_context`, memory items, knowledge items, citations, status, and truncation metadata.
3. Add memory adapter boundary and mem0-backed implementation for recall/write candidate handling.
4. Add memory governance metadata, scopes, TTL defaults, cleanup path, logs, and admin/debug visibility.
5. Add knowledge source metadata, chunk metadata, Milvus-backed retrieval, and `/knowledge/search`.
6. Add router/Invoker prefetch orchestration based on Agent Definition context.
7. Add controlled retrieval execution path for fixed workflow nodes with configured templates.
8. Upgrade Evidence Provider scheduling around fixed questions and future multiple providers.
9. Thread recalled memory and knowledge through Context Pack budgeting before invocation.
10. Add tests for schema validation, prefetch assembly, timeout degradation, scope/permission enforcement, fixed-question precedence, and observability.
11. Update docs and visual test UI debug/admin surfaces.

Rollback strategy: Agent context declarations can be ignored behind feature flags, leaving existing routing and invocation paths unchanged. Existing fixed-question Evidence Provider behavior must remain compatible throughout the migration.

## Open Questions

None for proposal readiness. Adapter-specific choices such as RAG-Anything integration, alternate vector stores, and user-facing memory notifications should be handled in later changes.
