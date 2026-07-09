## Why

M5 Memory and M6 Knowledge are no longer isolated router enhancements: they must serve the central router, Invoker, and downstream Agents through one governed context contract. The project needs a stable way for Agent Definitions to declare what memory and knowledge they need, so the platform can prefetch, budget, audit, and pass deterministic `memory_context` and `knowledge_context` fields without exposing free-form retrieval to every Agent.

## What Changes

- Add an `AgentDefinition.context` contract for declaring memory and knowledge requirements per Agent.
- Add `memory_context` assembly for every Agent route/invocation when memory prefetch is configured, with structured `items` plus `summary`.
- Add `knowledge_context` assembly only when the Agent explicitly enables knowledge prefetch, with structured `items`, `summary`, and citations.
- Keep `/knowledge/search` as a general-purpose governed retrieval API, not strongly bound to `agent_id`; callers identify `caller_type`, optional `caller_id`, and `purpose`.
- Add controlled retrieval mode for fixed workflow nodes that call `/knowledge/search` or memory recall through predefined templates, not model-selected arbitrary tool use.
- Use mem0 as the default memory strategy layer for extraction, search, update, and merge behavior while keeping open-intent-router responsible for tenant enablement, scopes, TTL, permissions, redaction, audit, and Agent visibility.
- Keep fixed questions inside M6 as a special `EvidenceProvider` path for question-to-intent mapping only; strong fixed-question matches remain deterministic route overrides.
- Standardize PostgreSQL + Milvus as the expected development infrastructure for memory and knowledge metadata/vector retrieval in this change.
- Do not add fixed FAQ answers, unrestricted Agent self-retrieval, or RAG-Anything integration in this change.

## Capabilities

### New Capabilities

- `agent-context-contract`: Agent Definition context declarations and deterministic context fields passed to Agents.
- `memory-context-governance`: Memory scopes, mem0-backed recall/write behavior, TTL, conflict handling, and `memory_context` assembly.
- `knowledge-context-retrieval`: Governed knowledge search, knowledge source access, prefetch/controlled retrieval modes, and `knowledge_context` assembly.
- `evidence-provider-scheduling`: Multiple Evidence Provider scheduling, fixed-question special handling, and route-stage evidence governance.

### Modified Capabilities

- None. Current repository specs are still represented by change-local specs, so this change introduces new M5/M6 capability specs rather than modifying archived baselines.

## Impact

- Backend schema:
  - Extend Agent Definition with `context.memory` and `context.knowledge` configuration.
  - Add `memory_context` and `knowledge_context` structures to invocation input / context handoff paths.
  - Add memory item, knowledge item, citation, retrieval status, truncation, and debug metadata schemas.
- Backend services:
  - New or extended memory recall/write services, mem0 adapter boundary, and memory cleanup behavior.
  - New or extended knowledge search service, knowledge source registry, and Milvus/PostgreSQL-backed retrieval.
  - Router/Invoker prefetch orchestration and controlled retrieval execution.
  - Evidence Provider scheduling upgrade around the existing fixed-question provider.
- APIs:
  - Add governed memory recall/write/admin APIs as needed.
  - Add `/knowledge/search` as a general retrieval endpoint with caller/purpose metadata and policy enforcement.
  - Keep fixed-question route overrides compatible with the existing Evidence Provider contract.
- Infrastructure:
  - PostgreSQL remains the metadata and log store.
  - Milvus stores separate memory and knowledge vector collections.
  - mem0 is used through an adapter and does not own open-intent-router permission or lifecycle rules.
- Frontend / test UI:
  - Debug/admin surfaces show memory writes, recall results, retrieval results, and context fields.
  - End users are not shown automatic memory-write prompts in the normal chat UI for this change.
- Tests:
  - Agent context schema validation, prefetch behavior, context field assembly, memory TTL/scope behavior, knowledge permissions, fixed-question precedence, timeout degradation, and observability.
