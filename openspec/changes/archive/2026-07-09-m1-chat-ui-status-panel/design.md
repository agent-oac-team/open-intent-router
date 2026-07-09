## Context

`open-intent-router` already has a local Vite + React test UI under `web/`. The current UI can configure Agents, send route-only and route-and-invoke requests, inspect RouteResponse, InvocationResult, Plan, and Agent Event data. It also already follows the newer Plan contract by displaying a plan whenever `route.plan` exists, regardless of `decision.action`.

The gap is presentation rather than backend capability. The current center panel still renders each request as a technical turn card with "第 N 轮", decision action, target Agent, and invocation result mixed into the conversation area. The capability design document defines M1 as a frontend-only step: the middle panel should simulate a normal user chat, while route, plan, evidence, invocation, and debug status should move into a unified right-side status panel.

Constraints:

- The project must remain generic and open-source friendly.
- M1 MUST NOT add backend API fields or change route schemas.
- M1 uses `decision.message` as the assistant bubble source.
- M3 will later add top-level `assistant_message`; M1 should leave a small frontend extraction helper so the future migration is localized.
- The existing Agent configuration panel, route-only mode, route-and-invoke mode, plan actions, and event submission remain available.

## Goals / Non-Goals

**Goals:**

- Make the center test experience behave like a chat transcript with user and assistant bubbles.
- Keep debug state visible, but move it out of the conversation transcript.
- Add a right-side tabbed inspector with Route, Plan, Context, Memory, Evidence, and Debug groups.
- Preserve all existing test workflows: route-only, route-and-invoke, plan refresh, plan execution actions, resume, cancel, and Agent Event JSON submission.
- Keep the implementation frontend-only and compatible with current backend responses.
- Update frontend tests and visual test UI documentation to match the new interaction model.

**Non-Goals:**

- Do not add `assistant_message` to backend responses in M1.
- Do not implement tag-based candidate filtering, slot extraction, Context Pack, Memory API, or multi Evidence Provider scheduling in this change.
- Do not remove `decision.message` from the API.
- Do not redesign Agent CRUD or runtime provider configuration.
- Do not build a production Host App chat client; this remains a local developer test UI.

## Decisions

### Decision 1: Use A Frontend Chat Message Model

The UI will replace `ConversationTurn` as the primary center transcript model with a chat message model such as `ChatMessage`.

The message model should include:

- `id`
- `role`: `user`, `assistant`, or `system`
- `content`
- `status`: `pending`, `completed`, or `failed`
- `createdAt`
- optional `requestId` or references to the latest route/result for diagnostics

When a user submits text, the UI immediately appends a user bubble and an assistant pending state. When the route call completes, the assistant bubble is populated from the response. If the call fails, the UI updates the assistant/system bubble with the error.

Rationale:

- It matches normal chat behavior.
- It keeps the center panel readable for business-facing demos and manual testing.
- It avoids forcing debug metadata into user-visible transcript state.

Alternative considered:

- Keep the existing turn cards and only restyle them. This would not solve the core problem because route state and invocation details would still be mixed into the conversation.

### Decision 2: Keep M1 Assistant Text Derived From `decision.message`

M1 will use a frontend helper to derive the assistant bubble text from current response fields:

1. Prefer `route.decision.message`.
2. Fall back to `route.decision.reason` when message is empty.
3. Fall back to a generic completion message for `silent` or unusual empty responses.

The helper should be named and isolated so M3 can switch it to prefer top-level `assistant_message` without rewriting chat rendering.

Rationale:

- This follows the confirmed M1/M3 evolution path in `docs/中控能力设计文档.md`.
- It avoids introducing backend protocol churn before M3.
- It gives the user a real answer in the chat area while preserving route details in the inspector.

Alternative considered:

- Build assistant bubbles by concatenating `decision.message`, `next_action.message`, and invocation result messages. Rejected because the design document explicitly narrows those fields to different UI areas.

### Decision 3: Use The Latest Response As Inspector State

The right-side inspector will render the latest route response, invocation result, plan state, event response, and raw payloads. Historical debug details do not need to remain embedded in every chat message for M1.

Rationale:

- The local test UI is primarily for iterative manual testing.
- Keeping the latest detailed state on the right mirrors how users inspect current routing status.
- It reduces front-end state complexity.

Alternative considered:

- Store a full debug snapshot per chat message and allow selecting any historical turn. This is useful, but it is better suited to M8 replay and diagnostics rather than M1.

### Decision 4: Tabbed Status Inspector Groups Debug Information

The right rail will become a single status inspector with tabs in this order:

1. Route
2. Plan
3. Context
4. Memory
5. Evidence
6. Debug

Route and Plan are the primary tabs. Context and Memory may initially show empty or "not available yet" states until M4/M5 are implemented. Evidence will show current `route.context.evidence` and related evidence context when present. Debug will contain raw RouteResponse, InvocationResult, Agent Event JSON submission, Event response, UI handoff output, and other JSON blocks.

Rationale:

- It matches the module roadmap and leaves visible placeholders for future capabilities.
- It keeps plan actions close to plan state.
- It prevents right-side panels from growing as separate stacked cards.

Alternative considered:

- Keep separate `ResultPanel` and `PlanPanel`. Rejected because the design document calls for one unified right-side state area.

### Decision 5: Keep Advanced Request Context Secondary

Advanced request fields such as `source`, `current_agent`, `frontend_context`, `plan_id`, and `step_id` remain in the conversation panel, but collapsed below the chat input and controls.

Rationale:

- Developers still need these fields to test agent chat, plan control, and host context.
- They should not compete with the primary chat transcript.

Alternative considered:

- Move advanced request fields to the right inspector. Rejected because these fields are request controls, while the right inspector should primarily show response and execution state.

## Risks / Trade-offs

- [Risk] Users may expect the assistant bubble to include invocation output in route-and-invoke mode. -> Mitigation: M1 keeps invocation output in the Debug or Route/Plan inspector and documents that the chat bubble comes from `decision.message` until M3 unifies user-visible output.
- [Risk] Context and Memory tabs may look empty before M4/M5. -> Mitigation: Show explicit empty states that say the current response has no Context Pack or Memory data, without implying an error.
- [Risk] Refactoring `App.tsx` can create a large component file. -> Mitigation: Keep the first implementation focused, but extract small helpers/components when it improves testability, such as assistant message extraction and inspector tab rendering.
- [Risk] The current dark UI could become visually dense after adding tabs. -> Mitigation: Use compact tabs, stable panel heights, collapsed JSON blocks, and preserve responsive behavior for narrower screens.

## Migration Plan

1. Add the chat message state and assistant text extraction helper.
2. Replace `ConversationTimeline` turn cards with chat bubbles and a normal chat transcript.
3. Refactor `ResultPanel` and `PlanPanel` into a unified status inspector with tabs.
4. Move existing route summary, evidence, invocation preview/result, UI handoff, full JSON, plan controls, and event submission into the appropriate tabs.
5. Update frontend tests for chat bubbles, no visible "第 N 轮", Plan display by presence, and tabbed status rendering.
6. Update `docs/visual-test-ui.md` to describe the chat-style test UI and status tabs.
7. Run `cd web && npm run test` and `cd web && npm run build`.

Rollback is straightforward because this change is frontend-only: restore the previous `ConversationPanel`, `ConversationTimeline`, `ResultPanel`, and `PlanPanel` implementation if needed. Backend APIs and stored data are unaffected.

## Open Questions

None for M1. The confirmed design keeps backend responses unchanged and uses `decision.message` until M3 introduces `assistant_message`.
