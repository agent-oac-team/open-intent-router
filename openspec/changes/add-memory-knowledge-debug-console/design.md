## Context

The current visual test UI keeps chat messages as a flat list and stores only the latest `routeResponse` / `invokeResponse` at page level. The right-side status inspector therefore explains the most recent request, not the selected conversation turn. This is enough for single-shot debugging, but it is weak once memory and knowledge retrieval become part of normal routing and invocation.

The backend already exposes the data needed for the requested P0/P1 scope:

- `/api/v1/route` and `/api/v1/route-and-invoke` include invocation preview data, including `memory_context` and `knowledge_context` when assembled for an Agent.
- `/api/v1/memories/debug` exposes memory items, mem0/history events, and adapter metadata.
- `/api/v1/knowledge/debug` exposes knowledge sources, chunks, retrieval logs, and metadata.
- `/api/v1/runtime/config` exposes high-level memory and knowledge backend status.

This change should use those existing contracts first. Durable context trace, write management, delete, reindex, and production-grade audit workflows are intentionally deferred.

## Goals / Non-Goals

**Goals:**

- Make each conversation turn carry its own route response, invoke result, memory context, and knowledge context.
- Show a compact memory/knowledge usage summary under each completed assistant turn.
- Let the user select a turn and inspect that turn in the right-side status panel.
- Split the current combined Memory tab into separate Memory and Knowledge tabs.
- Add read-only memory and knowledge debug management views using existing debug APIs.
- Keep the UI useful for repeated local debugging and smoke validation without introducing new backend dependencies.

**Non-Goals:**

- No persistent backend `context_trace` table or replayable audit history in this change.
- No memory edit/delete/merge UI.
- No knowledge source/chunk write API, upload flow, delete flow, or Milvus reindex operation.
- No direct Milvus content editing; the UI should treat Milvus as an index and PostgreSQL as canonical knowledge metadata/chunk storage.
- No multi-user collaboration or shared conversation history.

## Decisions

### Decision 1: Introduce a frontend `ConversationTurn` model

Represent the center conversation as turns rather than a flat message list:

```ts
type ConversationTurn = {
  id: string;
  requestId?: string;
  userMessage: ChatMessage;
  assistantMessage: ChatMessage;
  routeResponse?: RouteResponse | null;
  invokeResponse?: InvocationResult | null;
  memoryContext?: JsonRecord | null;
  knowledgeContext?: JsonRecord | null;
};
```

The existing visual UI can still render user and assistant bubbles, but the state owner should be `conversationTurns`. The selected turn drives the right-side inspector. This avoids backend changes while making per-turn trace visible immediately.

Alternative considered: keep flat `chatMessages` and add a map from `messageId` to response data. That would be less disruptive but more fragile, because the UI concept is now a turn, not an individual message.

### Decision 2: Derive per-turn memory/knowledge trace from route invocation input

For route-and-invoke, extract:

- `route.invocation.input.memory_context`
- `route.invocation.input.knowledge_context`
- related metadata from `route.context.metadata.agent_context`

For route-only, use the same extraction when invocation preview exists; otherwise show an explicit empty/disabled state for the selected turn.

Alternative considered: call `/memories/debug` and `/knowledge/debug` after every message to reconstruct what was used. That would confuse global repository state with per-turn use, and it would be slower.

### Decision 3: Split Memory and Knowledge inspector tabs

The current `MemoryTab` shows both memory and knowledge context. Split into:

- `Memory`: memory status, item count, scopes, confidence, relevance, source, TTL, provider metadata, errors.
- `Knowledge`: knowledge status, item count, citations, source IDs, scores, titles, URIs, denied source IDs, vector backend/collection metadata, errors.

Memory represents user/task state; knowledge represents evidence and citations. Keeping them separate makes debugging and later admin workflows cleaner.

Alternative considered: keep a single "Context" or "Memory / Knowledge" tab. That saves screen space but hides the different governance semantics.

### Decision 4: Add read-only debug management views before write management

The management area should initially expose:

- Memory debug: filters for `user_id`, `tenant_id`, `agent_id`, `scopes`, `limit`; tables/lists for items and events.
- Knowledge debug: filters for `source_ids`, `caller_type`, `caller_id`, `purpose`, `tenant_id`, `limit`; tables/lists for sources, chunks, logs.
- Runtime summary: memory provider, mem0 collection, knowledge backend, and Milvus collection when available.

Write flows are intentionally deferred because memory write governance, knowledge source mutation, and reindex have different risk profiles.

Alternative considered: implement manual write/edit/delete in the same change. That would slow down the P0 per-turn debugging goal and expand backend scope.

### Decision 5: Do not expose secrets or raw credentials

Debug management views may show provider names, collection names, URI/path-like non-secret values, model names, dimensions, item IDs, source IDs, and status fields. They must not show API keys, database passwords, Milvus tokens, or unredacted connection strings.

## Risks / Trade-offs

- [Risk] Frontend-only per-turn trace disappears on page refresh. -> Mitigation: label this as a local debugging console behavior; defer durable trace to a future backend audit change.
- [Risk] Debug API data is global/current state and may be mistaken for per-turn use. -> Mitigation: visually separate "Selected turn trace" from "Debug management"; do not merge debug lists into turn badges.
- [Risk] Route-only flows may have no invocation input. -> Mitigation: show disabled/empty context state with a short status rather than guessing from repository debug data.
- [Risk] Knowledge debug pages may encourage treating Milvus as the source of truth. -> Mitigation: UI copy and labels should present PostgreSQL source/chunk/log data as canonical and Milvus as backend/index metadata only.
- [Risk] Additional frontend state can make the existing single-file `App.tsx` harder to maintain. -> Mitigation: keep extraction helpers and display components small; consider component extraction only if the local pattern already supports it or the file becomes unwieldy.

## Migration Plan

1. Introduce the frontend `ConversationTurn` state model while preserving current visual behavior.
2. Route message send success/failure updates through selected/created turn instead of global latest-only state.
3. Make the right-side inspector read from `selectedTurn` with fallback to latest turn.
4. Add debug API client methods and read-only management views.
5. Update frontend tests and docs.

Rollback is straightforward: revert the frontend change and retain the existing backend APIs, since no database migration or backend contract change is required.

## Open Questions

- Should the management views live as new left-nav sections inside the existing test UI, or as tabs/panels within the current workspace?
- Should a user click on a chat turn to select it, or should the status inspector follow hover/focus plus a pinned selection?
- Should memory/knowledge badges be shown under both route-only and route-and-invoke assistant turns, or only when invocation input exists?
