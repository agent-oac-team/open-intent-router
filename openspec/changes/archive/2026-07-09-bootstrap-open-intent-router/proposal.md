## Why

The existing `intent_recon_sys` has proven the value of an intent-recognition control plane, but it is tightly coupled to OAC, private-banking scenarios, Feishu registry sync, Coze-style bot routing, and business-specific agents. This change turns the reusable core into `open-intent-router`: a lightweight, pluggable framework for routing user intent to Agents, Tools, and Workflows in any host application.

The project now has a concrete requirements document in `docs/开源意图识别项目需求分析文档.md`; the next step is to convert those requirements into an implementation-ready refactor plan without carrying OAC-specific assumptions into the open-source core.

## What Changes

- Introduce a new open-source backend skeleton for `open-intent-router` based on FastAPI, Pydantic schemas, SQLAlchemy storage, and a provider-based service architecture.
- Replace OAC-specific central-routing concepts with generic `Intent Router`, `Agent Registry`, `Agent Invoker`, `Session Context`, `Plan`, `Agent Run`, and `Evidence Provider` concepts.
- Provide `POST /api/v1/route` and `POST /api/v1/route-and-invoke` as the primary runtime APIs.
- Implement an OpenAI-compatible LLM router by default, with Mock LLM support for tests and a DeepSeek-compatible example configuration.
- Implement a generic Agent Registry with full CRUD, SQLite as the local development database, PostgreSQL as the production-preferred database, and YAML/JSON registry fallback.
- Implement basic Agent invokers for `http`, `local_function`, `mock`, and `ui_handoff`; defer Coze, Dify, FastGPT, LangGraph, and other platform adapters.
- Add route-and-invoke execution records through `AgentRun`, `AgentResult`, and `AgentEvent` models.
- Add session context reconstruction from chat messages, events, recent results, and plans; keep complex `session_state` out of MVP behavior.
- Add plan creation, confirmation, progression, and event-driven step updates for multi-step tasks.
- Introduce an optional Evidence Provider plugin contract, with fixed-question mapping and knowledge retrieval as the first extension direction.
- Add route logs, run logs, structured errors, health/readiness states, and admin-token protection for management APIs.
- Remove or isolate OAC, banking, Feishu, Coze, Milvus, and business-agent assumptions from the core implementation; document OAC migration separately rather than embedding it in core code.
- **BREAKING** for code migrated from `intent_recon_sys`: top-level `bot_id`, `route_path`, OAC naming, Feishu registry sync, banking seed agents, and DeepSeek-specific settings are replaced by generic `invocation`, `ui_handoff`, provider configuration, sample agents, and OpenAI-compatible LLM settings.

## Capabilities

### New Capabilities

- `intent-routing`: Runtime route API, structured router input/output, LLM routing, candidate-agent filtering, route validation, and route-only behavior.
- `agent-registry`: Generic Agent Definition model, database/file registry sources, full CRUD, reload, fallback behavior, access policy filtering, and sanitized public views.
- `agent-invocation`: Explicit invoke and route-and-invoke APIs, basic invokers, run/result/event records, input/output validation, and `ui_handoff` behavior.
- `session-context`: Chat history, conversation events, recent agent results, artifact references, and bounded context-window construction for routing.
- `plan-orchestration`: Multi-step plan persistence, confirmation, cancellation, step progression, and event-driven status updates.
- `evidence-provider`: Optional plugin contract for fixed-question intent mapping and evidence retrieval before LLM routing.
- `observability-and-admin`: Health/readiness, degraded registry state, route logs, run logs, structured errors, admin authentication, and safe logging rules.

### Modified Capabilities

- None. This repository currently has only the requirements document and no existing OpenSpec capability specs.

## Impact

- New project files will be added under `open_intent_router/` for the backend package, configuration examples, tests, and developer documentation.
- Existing `intent_recon_sys` code should be treated as source reference only; implementation must avoid direct OAC, Feishu, banking, and Coze coupling in core modules.
- Runtime APIs will be generic and host-application oriented:
  - `POST /api/v1/route`
  - `POST /api/v1/route-and-invoke`
  - `POST /api/v1/invoke`
  - Agent Registry CRUD and reload endpoints
  - Session, event, run, and plan endpoints
- Storage will use shared SQLAlchemy models for SQLite and PostgreSQL, plus an in-memory backend for tests where appropriate.
- Configuration will move to generic environment variables such as `DATABASE_URL`, `REGISTRY_BACKEND`, `REGISTRY_FILE_PATH`, `ROUTER_LLM_PROVIDER`, `ROUTER_LLM_BASE_URL`, `ROUTER_LLM_MODEL`, `ROUTER_LLM_API_KEY`, `ROUTE_MODE`, and `ADMIN_API_TOKEN`.
- The MVP must remain API-first and backend-focused; no frontend UI, Feishu sync, Milvus indexing, third-party bot-platform adapters, distributed workers, or SaaS control plane are included in this change.
