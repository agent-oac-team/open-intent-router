## 1. Schema And Contracts

- [x] 1.1 Add optional top-level `assistant_message` to `RouteResponse` while keeping existing route response fields backward compatible.
- [x] 1.2 Update response serialization and TypeScript types so route-only, route-and-invoke, and route-and-execute expose `assistant_message`.
- [x] 1.3 Add or update API docs for `assistant_message`, `decision.message`, `next_action.message`, `AgentInvocationResult.message`, and `decision.reason` responsibilities.
- [x] 1.4 Confirm `RouteAction` validation covers `reply`, `clarify`, `open_agent`, `continue_agent`, `exit_agent`, `show_plan`, `unsupported`, and `silent`.

## 2. Assistant Message Generation

- [x] 2.1 Add a Router helper that derives `assistant_message` from the normalized route response without requiring clients to inspect debug fields.
- [x] 2.2 Generate user-visible clarification text for missing required inputs and low-confidence clarification responses.
- [x] 2.3 Generate stable assistant text for plan, open-agent, continue-agent, exit-agent, unsupported, reply, and silent responses.
- [x] 2.4 Ensure route-and-invoke and route-and-execute preserve route-level `assistant_message` while leaving invocation and execution summaries in result payloads.
- [x] 2.5 Normalize LLM-provided `assistant_message`, fill it when missing, and prevent Debug / Result / Plan internal messages from being concatenated into chat output.

## 3. Plan Contract Migration

- [x] 3.1 Adjust fallback multi-task plan generation so it returns a valid `plan` without forcing `decision.action=show_plan`.
- [x] 3.2 Keep `show_plan` validation and compatibility behavior for legacy outputs.
- [x] 3.3 Ensure execution policy and `next_action` are applied whenever a plan exists, regardless of `decision.action`.
- [x] 3.4 Update frontend plan display logic tests to assert that `plan` presence, not `show_plan`, drives the Plan tab.

## 4. Candidate Filtering

- [x] 4.1 Add a generic tag extraction helper for Agent `tags`, `capabilities`, `trigger` terms, `metadata.intent_tags`, and `metadata.routing_tags`.
- [x] 4.2 Add Router or Registry filtering that runs after availability filtering and before Evidence Provider / LLM calls.
- [x] 4.3 Record filtered candidate IDs and filter reasoning in route context metadata or route logs for Debug visibility.
- [x] 4.4 Ensure Evidence Provider and LLM inputs never include Agents outside the filtered candidate set or outside the user's available Agent set.
- [x] 4.5 Implement no-match fallback to all currently available Agents and record `tag_filter=no_match_fallback_all_available` in context metadata or Route Log.
- [x] 4.6 Add tests for tag hits, no-hit fallback behavior, no-available-Agent behavior, and access-policy isolation.

## 5. Low Confidence And Slot Clarification

- [x] 5.1 Add a conservative configurable low-confidence clarification threshold.
- [x] 5.2 Ensure low-confidence clarification records confidence, threshold, and reason in Route or Debug metadata.
- [x] 5.3 Ensure missing required inputs produce `decision.action=clarify`, no invocation preview, and an `assistant_message` asking for the missing fields.
- [x] 5.4 Add optional `next_action.type=collect_input` metadata for structured missing-field collection when useful for Host App flows.
- [x] 5.5 Ensure route-only, route-and-invoke, and route-and-execute all stop before invocation or execution when required inputs are missing.
- [x] 5.6 Add regression tests for low confidence, required input present, and required input missing.

## 6. LLM Prompt And Clients

- [x] 6.1 Update the default router prompt to describe `assistant_message` and the scoped duties of existing message fields.
- [x] 6.2 Update Mock LLM outputs to include or allow backend completion of `assistant_message` across representative actions.
- [x] 6.3 Update OpenAI-compatible parsing tests for route outputs with and without `assistant_message`.
- [x] 6.4 Add tests that old LLM outputs lacking `assistant_message` are normalized by the backend.

## 7. Frontend And Documentation

- [x] 7.1 Update the chat assistant text helper to prefer `assistant_message` and fall back to `decision.message`.
- [x] 7.2 Keep `next_action.message`, invocation result messages, and `decision.reason` in right-side status areas rather than chat bubbles.
- [x] 7.3 Update `docs/visual-test-ui.md` to describe the M3 chat source migration.
- [x] 7.4 Update `docs/中控能力设计文档.md` only if implementation confirms a design correction or completed-state note.

## 8. Verification

- [x] 8.1 Run `.venv/bin/python -m pytest` for backend route, schema, prompt, LLM, and invocation coverage.
- [x] 8.2 Run `cd web && npm run test` for frontend behavior.
- [x] 8.3 Run `cd web && npm run build` for frontend type and production build verification.
- [x] 8.4 Run `openspec validate m3-intent-slots --strict`.
