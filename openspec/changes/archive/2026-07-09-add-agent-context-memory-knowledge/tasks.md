## 1. Schema And Configuration

- [x] 1.1 Extend Agent Definition schema with `context.memory` and `context.knowledge` configuration.
- [x] 1.2 Add validation for context modes: `disabled`, `prefetch`, and `controlled_retrieval`.
- [x] 1.3 Add memory scope enum values: `user_preference`, `stable_fact`, `task_memory`, `artifact_reference`, and `session_summary`.
- [x] 1.4 Add structured `memory_context` schemas with `summary`, `items`, `status`, and truncation metadata.
- [x] 1.5 Add structured `knowledge_context` schemas with `summary`, `items`, `citations`, `source_ids`, `status`, and truncation metadata.
- [x] 1.6 Add settings for memory enablement, mem0 adapter configuration, prefetch timeouts, TTL defaults, Milvus collections, and PostgreSQL-backed metadata.
- [x] 1.7 Update example Agent YAML to demonstrate memory prefetch, knowledge disabled default, knowledge prefetch, and controlled retrieval.

## 2. Memory Store And Governance

- [x] 2.1 Add memory item, memory event, recall log, and write decision models or repository interfaces.
- [x] 2.2 Add mem0 adapter boundary for add/search/update behavior without exposing mem0 directly to router or Invoker code.
- [x] 2.3 Implement mem0-backed memory recall with user, tenant, subject, scope, metadata filters, query, and limit inputs.
- [x] 2.4 Implement memory write candidate handling for messages, Agent results, and Plan results.
- [x] 2.5 Apply OIR policy after mem0 extraction for tenant enablement, scope, visibility, confidence, sensitivity, and Agent eligibility.
- [x] 2.6 Implement scope-specific TTL defaults: 14 days for task memory, session summary, and artifact references; long-lived defaults for user preferences and stable facts.
- [x] 2.7 Add cleanup behavior for expired memory items and associated vector records.
- [x] 2.8 Add conflict/session override metadata when current input conflicts with recalled long-term memory.
- [x] 2.9 Add memory recall and write observability data for route/run logs and debug/admin views.

## 3. Knowledge Retrieval And Source Policy

- [x] 3.1 Add knowledge source, chunk, citation, retrieval log, and policy metadata models or repository interfaces.
- [x] 3.2 Add Milvus-backed knowledge vector search with PostgreSQL metadata hydration.
- [x] 3.3 Implement `/knowledge/search` as a general governed API with `caller_type`, optional `caller_id`, `purpose`, user, tenant, subject, source filters, and query inputs.
- [x] 3.4 Enforce knowledge source policy at KnowledgeService, treating Agent-declared source IDs as requested sources rather than final access proof.
- [x] 3.5 Return structured knowledge items, citations, source IDs, retrieval status, and truncation metadata.
- [x] 3.6 Add timeout and provider-error degradation for knowledge retrieval.
- [x] 3.7 Record selected sources, denied sources, hit metadata, errors, and truncation in retrieval logs.

## 4. Prefetch And Controlled Retrieval

- [x] 4.1 Add a context assembly service that reads Agent Definition context and orchestrates memory and knowledge retrieval.
- [x] 4.2 Run memory prefetch for configured Agents on every route/invocation before calling the target Agent.
- [x] 4.3 Keep knowledge prefetch disabled unless `context.knowledge.mode=prefetch` is explicitly configured.
- [x] 4.4 Execute memory and knowledge prefetch concurrently where possible and apply bounded timeouts.
- [x] 4.5 Pass prefetched memory and knowledge through Context Pack budget selection before invocation handoff.
- [x] 4.6 Attach stable `memory_context` and `knowledge_context` fields to invocation inputs and UI handoff previews.
- [x] 4.7 Implement controlled retrieval execution for fixed workflow nodes using configured query templates and allowed variables.
- [x] 4.8 Ensure target Agents cannot perform undeclared memory or knowledge retrieval through this change's interfaces.

## 5. Evidence Provider Scheduling

- [x] 5.1 Introduce an Evidence Provider scheduler that can evaluate provider applicability, priority, timeout, and errors.
- [x] 5.2 Preserve current fixed-question behavior through the scheduler.
- [x] 5.3 Keep strong fixed-question route overrides ahead of LLM routing and outside ordinary evidence snippet budget trimming.
- [x] 5.4 Keep weak fixed-question hints as context-only signals that do not shrink the access-filtered LLM candidate set.
- [x] 5.5 Record provider selection, skips, errors, weak hints, and strong overrides in route debug/log metadata.

## 6. APIs, UI, And Documentation

- [x] 6.1 Add or update memory recall, memory management, and memory debug/admin APIs needed by the governance model.
- [x] 6.2 Add debug/admin UI states for recalled memory, memory writes, memory conflicts, knowledge retrieval, denied sources, and retrieval timeouts.
- [x] 6.3 Keep normal chat UI free of automatic memory-write prompts for this change.
- [x] 6.4 Update API documentation for Agent context config, `memory_context`, `knowledge_context`, `/knowledge/search`, and controlled retrieval.
- [x] 6.5 Update `docs/agent-definition.md`, `docs/evidence-provider.md`, and `docs/中控能力设计文档.md` with the finalized M5/M6 boundaries.

## 7. Tests And Validation

- [x] 7.1 Add schema tests for Agent context config, memory scopes, knowledge modes, and invalid combinations.
- [x] 7.2 Add memory prefetch tests for successful recall, empty recall, timeout degradation, scope filtering, TTL behavior, and conflict recording.
- [x] 7.3 Add memory write tests for low-risk auto-write, policy rejection, debug visibility, and cleanup.
- [x] 7.4 Add knowledge search tests for caller metadata, source policy enforcement, empty hits, denied sources, citations, and timeout degradation.
- [x] 7.5 Add invocation tests proving `memory_context` and `knowledge_context` are attached as structured fields with summaries.
- [x] 7.6 Add controlled retrieval tests for template rendering, allowed variables, denied sources, and downstream structured output.
- [x] 7.7 Add Evidence Provider scheduler tests for fixed-question precedence, weak hint behavior, provider skip reasons, and provider errors.
- [x] 7.8 Add Context Pack budget tests covering memory and knowledge truncation before invocation.
- [x] 7.9 Run backend regression tests and OpenSpec validation for the completed implementation.
