## Why

The current visual test UI is useful for debugging raw route and invocation payloads, but the center conversation area still presents each request as a numbered technical turn that mixes user input, route state, invocation result, and plan details. The capability design document identifies this as the first implementation step because a chat-like test surface will make later intent, context, memory, evidence, and plan work easier to observe without changing backend contracts.

## What Changes

- Refactor the center conversation test panel into a normal chat-style transcript with user and assistant bubbles.
- Use the current backend-visible user response text as the assistant bubble source: in M1 this is `decision.message`, with a frontend-only compatibility fallback such as `decision.reason` or a generic completion message when needed.
- Remove the visible "第 N 轮" turn-card presentation from the primary conversation area.
- Move route decision, plan state, context, memory, evidence, invocation, event, and raw JSON debugging into a right-side tabbed status inspector.
- Keep plan confirmation, execute, resume, cancel, refresh, and Agent Event JSON actions in the right-side status area.
- Keep advanced request context controls available, but make them secondary to the chat interaction.
- Preserve the existing route-only and route-and-invoke test modes.
- Do not add or change backend API fields in this module; `assistant_message`, tag filtering, Context Pack, Memory, and Evidence scheduling remain separate later modules.

## Capabilities

### New Capabilities

- `chat-conversation-console`: Chat-style local test conversation surface for sending user messages and displaying the current router-visible assistant response.
- `status-inspector-panel`: Right-side tabbed inspector for Route, Plan, Context, Memory, Evidence, and Debug state, including plan actions and raw diagnostic payloads.

### Modified Capabilities

- None. There is no archived baseline `openspec/specs/` directory yet; this change introduces M1-specific UI capabilities that build on the already implemented local visual test UI.

## Impact

- Frontend:
  - `web/src/App.tsx`
  - `web/src/styles.css`
  - `web/src/types.ts`
  - `web/src/App.test.tsx`
- Documentation:
  - `docs/App-Desc/interfaces/visual-test-ui.md`
  - `docs/App-Research/designs/中控能力设计文档.md` only if implementation discovers a confirmed design correction
- Backend:
  - No API contract change.
  - No schema change.
  - No change to route, invocation, plan, or event endpoints.
- Compatibility:
  - M1 continues to display assistant bubbles from `decision.message`.
  - Future M3 can switch the chat bubble source to top-level `assistant_message` with a fallback to `decision.message`.
