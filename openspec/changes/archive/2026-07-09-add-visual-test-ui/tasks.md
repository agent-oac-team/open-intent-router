## 1. Backend Support

- [x] 1.1 Add a secret-redacted runtime config response schema for UI bootstrap data.
- [x] 1.2 Add a read-only runtime config endpoint that returns provider, model, base URL, prompt file, route mode, registry backend, storage backend, registry status, and evidence provider status.
- [x] 1.3 Ensure runtime config endpoint never returns API keys, admin tokens, database passwords, or secret invocation config.
- [x] 1.4 Add backend tests for runtime config visibility and secret redaction.

## 2. Frontend Project Setup

- [x] 2.1 Create a `web/` Vite + React + TypeScript frontend application.
- [x] 2.2 Add frontend scripts for install, dev, build, lint or typecheck.
- [x] 2.3 Add API client utilities for health, ready, runtime config, route, route-and-invoke, Agent CRUD, plan fetch, and event submission.
- [x] 2.4 Configure local dev proxy or documented backend base URL handling.

## 3. Visual Test UI

- [x] 3.1 Build the main layout with service status, runtime routing status, Agent configuration panel, conversation test panel, and result inspector.
- [x] 3.2 Implement Agent list and detail views showing agent_id, name, type, enabled, capabilities, triggers, access policy, invocation, and ui_handoff summary.
- [x] 3.3 Implement Agent create/edit form for description, capabilities, trigger examples, required inputs, input/output schemas, access policy, invocation config, and ui_handoff config.
- [x] 3.4 Implement enable/disable and delete actions through Admin Agent APIs.
- [x] 3.5 Disable or warn for mutation actions when Registry is file-only or backend rejects mutations.

## 4. Conversation Testing

- [x] 4.1 Implement message input and session controls for session_id, source, user roles/groups/tenant/attributes, current_agent, frontend_context, plan_id, and step_id.
- [x] 4.2 Add route-only and route-and-invoke execution mode selector.
- [x] 4.3 Display route decision summary with action, target_agent_id, status, confidence, reason, and message.
- [x] 4.4 Display raw JSON viewers for RouteResponse and InvocationResult.
- [x] 4.5 Display Evidence, invocation preview, UI handoff route/params, Plan summary, and error details when present.
- [x] 4.6 Add client-side validation for source-specific requirements, especially `agent_chat` requiring `current_agent`.

## 5. Routing Mode and DeepSeek Configuration

- [x] 5.1 Add `.env.deepseek.example` or equivalent documented DeepSeek local env template using OpenAI-compatible fields.
- [x] 5.2 Update `.env.example` comments to show Mock defaults and DeepSeek LLM override values.
- [x] 5.3 Document original project DeepSeek field mapping to `ROUTER_LLM_*` fields.
- [x] 5.4 Ensure `.env` remains gitignored and no real API key is committed.
- [x] 5.5 Show current Mock/LLM mode in the UI and warn when UI-selected mode differs from backend runtime config.

## 6. Tests and Verification

- [x] 6.1 Add frontend unit tests or component tests for runtime status, Agent form behavior, and conversation request construction.
- [x] 6.2 Add an end-to-end local Mock route test that sends a message through the UI and verifies route decision rendering.
- [x] 6.3 Run backend test suite and frontend build/typecheck.
- [x] 6.4 Manually verify local startup instructions for backend and UI.

## 7. Documentation

- [x] 7.1 Update README with visual test UI startup commands and workflow.
- [x] 7.2 Add docs for configuring Agents/Intent metadata through the UI.
- [x] 7.3 Add docs for Mock vs OpenAI-compatible LLM routing behavior and backend restart requirement.
- [x] 7.4 Add docs for DeepSeek local configuration and secret handling.
