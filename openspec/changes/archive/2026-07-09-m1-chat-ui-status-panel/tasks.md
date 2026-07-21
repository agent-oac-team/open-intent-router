## 1. Chat Transcript Model

- [x] 1.1 Replace the primary `ConversationTurn` center-panel state with a chat message model that supports `user`, `assistant`, and `system` roles.
- [x] 1.2 Add a small assistant text extraction helper that uses `decision.message` in M1 and keeps a localized path for future `assistant_message` support.
- [x] 1.3 Update message submission so sending text immediately appends a user bubble and then updates an assistant bubble after route completion or failure.
- [x] 1.4 Ensure starting a new conversation clears chat messages, latest route response, invocation result, plan state, event response, and notices.

## 2. Center Chat UI

- [x] 2.1 Replace the visible numbered turn-card transcript with user and assistant chat bubbles.
- [x] 2.2 Remove "第 N 轮" wording from the primary conversation area.
- [x] 2.3 Keep route-only and route-and-invoke mode controls available near the chat input.
- [x] 2.4 Keep advanced request context controls available in a secondary collapsed section.
- [x] 2.5 Render errors as assistant or system chat feedback without exposing raw debug JSON in the transcript.

## 3. Status Inspector

- [x] 3.1 Replace separate right-side result and plan panels with one tabbed status inspector component.
- [x] 3.2 Add tabs in this order: Route, Plan, Context, Memory, Evidence, Debug.
- [x] 3.3 Move route decision summary, candidate Agent IDs, confidence, reason, message, and invocation preview into Route or Debug tab content.
- [x] 3.4 Move plan summary, execution policy, next action, steps, refresh, confirm-and-execute, execute, resume, and cancel controls into the Plan tab.
- [x] 3.5 Move evidence payload display into the Evidence tab.
- [x] 3.6 Add explicit empty states for Context and Memory until later modules provide data.
- [x] 3.7 Move full RouteResponse JSON, InvocationResult JSON, UI handoff output, Agent Event JSON editor, submit action, and event response into the Debug tab.

## 4. Styling And Responsiveness

- [x] 4.1 Add stable chat transcript, bubble, tab, and inspector styles in `web/src/styles.css`.
- [x] 4.2 Preserve the existing three-column desktop layout and responsive behavior on narrower screens.
- [x] 4.3 Keep JSON blocks collapsed by default where they are diagnostic rather than primary status.
- [x] 4.4 Verify long messages, long Agent IDs, and JSON content wrap or scroll without overlapping adjacent UI.

## 5. Tests

- [x] 5.1 Update frontend tests so submitting a message renders user and assistant chat bubbles.
- [x] 5.2 Add a regression assertion that the primary conversation area no longer renders "第 1 轮" style labels.
- [x] 5.3 Add tests that Route and Plan tabs render expected route and plan state, including plan presence when `decision.action` is not `show_plan`.
- [x] 5.4 Add tests that invocation result and raw response JSON are available in the Debug tab rather than the chat transcript.
- [x] 5.5 Run `cd web && npm run test`.
- [x] 5.6 Run `cd web && npm run build`.

## 6. Documentation

- [x] 6.1 Update `docs/App-Desc/interfaces/visual-test-ui.md` to describe the chat-style conversation panel and right-side status tabs.
- [x] 6.2 Document that M1 assistant bubbles use `decision.message`, while M3 will migrate the source to `assistant_message`.
- [x] 6.3 Document that Context and Memory tabs are placeholders until their modules are implemented.
