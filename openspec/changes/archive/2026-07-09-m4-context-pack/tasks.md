## 1. Schema And Configuration

- [x] 1.1 Add Context Pack, Context Item, Context Budget, Context Usage, and selection summary schemas.
- [x] 1.2 Add configuration for default token budget, optional source budgets, per-item limits, and character-to-token conversion ratio.
- [x] 1.3 Add TypeScript types for Context Pack debug data used by the visual test UI.
- [x] 1.4 Document that M4 exposes Context Pack debug data through `RouteContext.metadata.context_pack` while preserving existing metadata compatibility.
- [x] 1.5 Add request / response schema for appending host-managed `ChatMessage` records, with generic `source`, `role`, `content`, `agent_id`, `agent_session_id`, `request_id`, `event_id`, and `metadata` fields.

## 2. Context Item Builders

- [x] 2.1 Convert current user input into a non-droppable Context Item.
- [x] 2.2 Convert current Agent context and active plan state into high-priority Context Items when present.
- [x] 2.3 Convert host history and Agent history into Context Items with source, role, timestamps, and priority.
- [x] 2.4 Convert recent Agent results and artifact refs into Context Items.
- [x] 2.5 Convert current Evidence payloads into Context Items without implementing new Evidence Provider scheduling.
- [x] 2.6 Preserve generic source and scope values for future Memory items without implementing Memory Store behavior.
- [x] 2.7 Add `POST /api/v1/sessions/{session_id}/messages` for the host to append child Agent replies or other host-managed chat messages through `ChatHistoryService`.
- [x] 2.8 Require or validate `agent_id` when appending `source=agent_chat` messages, and preserve optional `agent_session_id` for child Agent conversation scoping.
- [x] 2.9 Ensure router-recorded user inputs and host-written `role=agent` / `role=assistant` messages are both available to Agent history Context Item builders.

## 3. Budget And Selection

- [x] 3.1 Implement character count and approximate token estimation utilities.
- [x] 3.2 Apply default token budget and allowed request-level overrides.
- [x] 3.3 Implement item ordering by priority, relevance, recency, and source policy.
- [x] 3.4 Exclude or trim lower-priority items when budget is exceeded, while preserving current input and current task state.
- [x] 3.5 Record included, dropped, truncated, or summary-placeholder status and reasons for each selected candidate item.
- [x] 3.6 Add tests for enough-budget, over-budget, per-item truncation, and character-limit conversion cases.

## 4. Router And LLM Integration

- [x] 4.1 Build Context Pack inside the route flow before Evidence / LLM prompt construction where applicable.
- [x] 4.2 Store Context Pack selected items or summary under `RouteContext.metadata.context_pack` so prompt builders and the test UI can consume it.
- [x] 4.3 Update Mock LLM and OpenAI-compatible prompt payload tests to include Context Pack data.
- [x] 4.4 Keep legacy `RouteContext.metadata` context fields available during migration.
- [x] 4.5 Ensure Context Pack failures degrade safely to a routing error or old metadata path rather than silently dropping current input.
- [x] 4.6 Ensure M4 trimming uses truncation and summary placeholders only, without calling an additional LLM for summarization.

## 5. Observability And Logs

- [x] 5.1 Add Context Pack usage summary to Route Log data.
- [x] 5.2 Store budget values, usage source, included counts, dropped counts, source distribution, and drop reasons without unbounded raw text.
- [x] 5.3 Apply existing redaction behavior to Context Pack metadata before persistent logging or debug display.
- [x] 5.4 Add tests for Route Log context usage summaries and no unbounded long-history logging.

## 6. Visual Test UI And Documentation

- [x] 6.1 Update the Context tab to show budget usage, included item count, dropped item count, and source groups when Context Pack data exists.
- [x] 6.2 Show dropped, trimmed, or summary-placeholder item reasons in the Context tab.
- [x] 6.3 Keep an explicit empty Context tab state when Context Pack data is unavailable.
- [x] 6.4 Update `docs/App-Desc/contracts/api.md` and `docs/App-Desc/interfaces/visual-test-ui.md` for Context Pack, budget diagnostics, and the host-managed session message append API.
- [x] 6.5 Update `docs/App-Research/designs/中控能力设计文档.md` only if implementation confirms a design correction or completed-state note.

## 7. Verification

- [x] 7.1 Run `.venv/bin/python -m pytest` for backend context, router, LLM, and log coverage.
- [x] 7.2 Run `cd web && npm run test` for Context tab behavior.
- [x] 7.3 Run `cd web && npm run build` for frontend type and production build verification.
- [x] 7.4 Run `openspec validate m4-context-pack --strict`.
- [x] 7.5 Add backend tests for appending child Agent reply messages, retrieving them as Agent history, and including or trimming them through Context Pack selection.
