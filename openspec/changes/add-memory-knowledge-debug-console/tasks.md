## 1. Conversation Turn Trace

- [x] 1.1 Define frontend `ConversationTurn` and context trace helper types in `web/src/types.ts` or local App types.
- [x] 1.2 Replace flat chat-message ownership with `conversationTurns` while preserving the current conversation rendering.
- [x] 1.3 Update `sendMessage()` route and route-and-invoke success paths to store route response, invoke result, request ID, memory context, and knowledge context on the completed turn.
- [x] 1.4 Update request failure handling so failed turns do not reuse previous turn trace.
- [x] 1.5 Add selected-turn state and make new completed turns selected by default.
- [x] 1.6 Update new-conversation reset to clear conversation turns and selected-turn trace.

## 2. Per-Turn Conversation UI

- [x] 2.1 Add compact badges under each completed assistant turn for memory item count, knowledge item count, citation count, denied source count, and context status.
- [x] 2.2 Make conversation turns selectable from the chat UI and visually distinguish the selected turn.
- [x] 2.3 Handle route-only and missing invocation input states with explicit empty/disabled/unavailable messaging.

## 3. Right-Side Status Inspector

- [x] 3.1 Refactor `StatusInspector` to read route/invoke/context data from the selected turn, with a latest-turn fallback only when no turn is selected.
- [x] 3.2 Split the current combined Memory display into a Memory tab focused on memory context fields and raw JSON.
- [x] 3.3 Add a Knowledge tab focused on knowledge context fields, citations, source IDs, denied sources, scores, URIs, errors, and raw JSON.
- [x] 3.4 Ensure Memory and Knowledge tabs do not merge or duplicate each other's items.
- [x] 3.5 Keep Route, Plan, Context, Evidence, and Debug tabs aligned with the selected turn.

## 4. Read-Only Debug Management Views

- [x] 4.1 Add frontend API methods and types for `/api/v1/memories/debug` and `/api/v1/knowledge/debug`.
- [x] 4.2 Add a read-only memory debug management view with filters for `user_id`, `tenant_id`, `agent_id`, `scopes`, and `limit`.
- [x] 4.3 Display memory items, memory events, and non-sensitive metadata in the memory debug management view.
- [x] 4.4 Add a read-only knowledge debug management view with filters for `source_ids`, `caller_type`, `caller_id`, `purpose`, `tenant_id`, and `limit`.
- [x] 4.5 Display knowledge sources, chunks, retrieval logs, canonical storage labels, and non-sensitive metadata in the knowledge debug management view.
- [x] 4.6 Display runtime memory/knowledge backend status near debug management views.
- [x] 4.7 Show API failure states for debug views without clearing the selected turn trace.

## 5. Safety and Scope Guards

- [x] 5.1 Confirm the UI does not expose API keys, database passwords, or Milvus tokens from runtime/debug data.
- [x] 5.2 Keep edit, delete, upload, merge, and reindex controls absent or disabled for this read-only scope.
- [x] 5.3 Label Milvus as vector index metadata and PostgreSQL knowledge source/chunk/log data as canonical.

## 6. Tests and Documentation

- [x] 6.1 Add or update frontend tests covering per-turn trace storage, selecting older turns, and no previous-trace reuse on failure.
- [x] 6.2 Add or update frontend tests covering memory/knowledge badges and split Memory/Knowledge tabs.
- [x] 6.3 Add or update frontend tests covering memory and knowledge debug API rendering and failure states.
- [x] 6.4 Update Chinese documentation for the debug console, per-turn trace behavior, read-only management scope, and future durable trace boundary.
- [x] 6.5 Run frontend tests/build and relevant backend/OpenSpec validation commands.
