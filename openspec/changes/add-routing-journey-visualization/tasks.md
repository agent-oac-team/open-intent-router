## 1. Journey Projection Model

- [x] 1.1 Define journey node identifiers, waiting/active/completed/skipped/failed states, summaries, detail payloads, and overall journey status types in the frontend.
- [x] 1.2 Implement a pure selected-turn projection helper for empty, pending, failed, route-only, route-and-invoke, direct-reply, clarify, unsupported, and Agent-routed outcomes.
- [x] 1.3 Derive Memory and Knowledge participation summaries from the selected turn's existing context data without consulting global debug-management records.
- [x] 1.4 Map the selected turn's Memory Trace loading, pending, successful, not-triggered, and error states to the memory-formation journey node.
- [x] 1.5 Add Agent Registry friendly-name lookup with safe agent_id fallback, plus Plan step-count and existing step-status summaries.

## 2. Right-Side Journey Experience

- [x] 2.1 Add a Journey status-inspector tab before the existing technical tabs and make it the initial selected tab.
- [x] 2.2 Implement journey empty, overall-processing, completed-path, skipped-path, and failed-path views using the projection model.
- [x] 2.3 Render the plain-language vertical stages for question receipt, reference preparation, controller judgment, Agent handoff, Agent execution, response delivery, and memory formation.
- [x] 2.4 Show concise Memory/Knowledge participation indicators, actual Agent friendly names, route outcome explanations, and Plan collaboration summaries without exposing raw payloads.
- [x] 2.5 Add an accessible read-only node-detail dialog with close, Escape, supported backdrop-dismiss, focus, and long-value handling consistent with existing modal patterns.
- [x] 2.6 Preserve all existing Route, Plan, Context, Memory, Knowledge, Evidence, and Debug inspector behavior and selected-turn semantics.

## 3. Visual States and Responsive Layout

- [x] 3.1 Add stable vertical flow, connector, icon, label, summary, and waiting/active/completed/skipped/failed styles using the existing palette and Lucide icons.
- [x] 3.2 Add a restrained running-state animation that applies only to the known overall processing state and asynchronous Memory Trace state, with reduced-motion support.
- [x] 3.3 Constrain long Agent names, identifiers, and summaries so the desktop right rail remains stable and full values remain available in node details.
- [x] 3.4 Verify the journey and detail dialog stack cleanly on narrow viewports without horizontal page scrolling, clipped controls, or overlapping text.

## 4. Frontend Regression Coverage

- [x] 4.1 Add projection-helper tests for empty, pending, request failure, route-only, successful invocation, failed invocation, direct reply, clarify, and unsupported outcomes.
- [x] 4.2 Add tests proving pending requests do not advance internal stages by timers and global Memory/Knowledge debug records do not affect the selected-turn journey.
- [x] 4.3 Add tests for Memory/Knowledge participation, Memory Formation pending/success/skipped/failure, Agent friendly-name fallback, and Plan step summaries.
- [x] 4.4 Add component tests for the default Journey tab, selecting an older turn, retaining technical tabs, and opening/closing accessible node details.
- [x] 4.5 Add or update responsive assertions for long content and narrow viewport journey rendering where supported by the existing frontend test setup.

## 5. Documentation and Verification

- [x] 5.1 Update Chinese frontend documentation to explain the journey audience, plain-language stages, result-branch behavior, and selected-turn interaction.
- [x] 5.2 Document that “中控处理中” is an overall request state and completed internal stages are reconstructed from returned data rather than observed through a real-time trace contract.
- [x] 5.3 Run `cd web && npm run typecheck`, `cd web && npm run test`, and `cd web && npm run build`.
- [x] 5.4 Run `openspec validate add-routing-journey-visualization --strict` and resolve all validation findings.
