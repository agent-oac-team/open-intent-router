## 1. Project Foundation

- [x] 1.1 Create the Python package structure under `open_intent_router/` with `app/api`, `app/core`, `app/schemas`, `app/services`, `app/repositories`, `app/invokers`, `app/llm`, `app/plugins`, and `tests`.
- [x] 1.2 Add project metadata, runtime dependencies, dev dependencies, formatter/linter settings, and test configuration.
- [x] 1.3 Implement FastAPI application bootstrap, lifespan wiring, API router registration, and `/health`.
- [x] 1.4 Implement typed settings for app, database, registry, LLM, route mode, admin token, timeouts, and logging.
- [x] 1.5 Add example environment files and `config/agents.example.yaml` with generic sample Agents only.

## 2. Core Schemas And Errors

- [x] 2.1 Implement generic Agent Definition schemas including `invocation`, `ui_handoff`, `access_policy`, `input_schema`, `output_schema`, trigger, capabilities, and metadata fields.
- [x] 2.2 Implement route request, route response, decision, route context, invocation payload, plan, artifact reference, and structured error schemas.
- [x] 2.3 Implement Agent Run, Agent Result, Agent Event, chat message, conversation event, and route log schemas.
- [x] 2.4 Add schema validators for action invariants, artifact reference normalization, Agent type consistency, and required Agent fields.
- [x] 2.5 Add shared exception types and HTTP error mapping for validation, authentication, registry, LLM, invocation, and storage errors.

## 3. Storage And Repositories

- [x] 3.1 Implement SQLAlchemy database models for Agent definitions, chat messages, conversation events, Agent runs, Agent results, plans, plan steps, and route logs.
- [x] 3.2 Configure SQLite as the local default database and keep SQLAlchemy model compatibility with PostgreSQL.
- [x] 3.3 Implement database session wiring and local database initialization for development and tests.
- [x] 3.4 Implement repository interfaces for registry, messages, events, runs, results, plans, and route logs.
- [x] 3.5 Implement database-backed repositories for the MVP tables.
- [x] 3.6 Implement memory-backed repositories where needed for deterministic unit tests.

## 4. Agent Registry

- [x] 4.1 Implement YAML and JSON file Registry loading with schema validation and duplicate `agent_id` detection.
- [x] 4.2 Implement database Registry source with CRUD, enable/disable, delete, list, get, and upsert behavior.
- [x] 4.3 Implement `database`, `file`, and `hybrid` Registry modes.
- [x] 4.4 Implement hybrid fallback to file when database is unavailable and mark readiness as `degraded`.
- [x] 4.5 Prevent implicit file/database merges unless explicit fallback-on-empty or merge settings are enabled.
- [x] 4.6 Implement Registry cache and reload behavior.
- [x] 4.7 Implement access policy filtering by roles, groups, tenants, and user attributes.
- [x] 4.8 Implement sanitized Agent views for public APIs and LLM candidate prompts.
- [x] 4.9 Implement authenticated Agent Registry CRUD and reload API endpoints.

## 5. LLM Routing

- [x] 5.1 Implement `LLMClient` protocol and Mock LLM client.
- [x] 5.2 Implement OpenAI-compatible LLM client using configurable base URL, model, API key, and timeout.
- [x] 5.3 Add DeepSeek example configuration without hard-coding DeepSeek as the default provider.
- [x] 5.4 Implement route context prompt construction from request data, candidate Agents, session context, plan context, and Evidence Provider hints.
- [x] 5.5 Implement Router service for candidate loading, LLM call, structured output parsing, and route response creation.
- [x] 5.6 Implement route output post-validation for candidate target checks, `show_plan` plan presence, `continue_agent` consistency, and unsupported cases.
- [x] 5.7 Implement missing required input detection and `clarify` responses before invocation.
- [x] 5.8 Implement `POST /api/v1/route` route-only endpoint.

## 6. Agent Invocation

- [x] 6.1 Implement `AgentInvoker` protocol and invoker registry.
- [x] 6.2 Implement `MockAgentInvoker` with configured mock responses.
- [x] 6.3 Implement `HttpAgentInvoker` with method, URL, headers, body mapping, timeout, structured errors, and secret redaction.
- [x] 6.4 Implement `LocalFunctionInvoker` using only trusted in-process function registration.
- [x] 6.5 Implement `UiHandoffInvoker` that returns host handoff data without executing external code.
- [x] 6.6 Implement invocation input building from route decision, user input, slot extraction, frontend context, history, recent results, and artifacts.
- [x] 6.7 Implement output schema validation and invalid-output result recording.
- [x] 6.8 Implement Agent Run and Agent Result persistence around every invocation attempt.
- [x] 6.9 Implement `POST /api/v1/invoke` explicit invocation endpoint.
- [x] 6.10 Implement `POST /api/v1/route-and-invoke` endpoint.

## 7. Session Context And Events

- [x] 7.1 Implement chat message persistence for `host_chat`, `agent_chat`, `agent_event`, `plan_control`, and `system` sources.
- [x] 7.2 Implement conversation event persistence for routing, navigation, Agent events, and plan control events.
- [x] 7.3 Implement context builder with bounded host history, Agent history, recent events, recent results, active plans, frontend context, and artifact references.
- [x] 7.4 Implement current-Agent continuity support for new task, continue current, switch Agent, and exit Agent relations.
- [x] 7.5 Implement Agent event ingestion endpoint with idempotent `event_id` handling.
- [x] 7.6 Implement compatibility endpoint `POST /api/v1/events/agent` for generic Agent event callbacks.
- [x] 7.7 Implement session history query endpoints needed for debugging and integration tests.

## 8. Plan Orchestration

- [x] 8.1 Implement plan and plan step persistence from validated `show_plan` route outputs.
- [x] 8.2 Implement plan validation for step IDs, dependencies, target Agent availability, and dependency cycles.
- [x] 8.3 Implement plan inspect, confirm, cancel, and status update APIs.
- [x] 8.4 Implement step progression from Agent run completion and Agent event callbacks.
- [x] 8.5 Implement dependency output mapping from prior step results and artifact references into dependent step invocations.
- [x] 8.6 Implement blocked and failed plan states when required prior outputs are missing or Agent execution fails.

## 9. Evidence Provider

- [x] 9.1 Define the Evidence Provider interface for fixed-question matching and evidence retrieval.
- [x] 9.2 Implement no-op Evidence Provider for default disabled mode.
- [x] 9.3 Implement file-backed fixed-question provider for local development and tests.
- [x] 9.4 Support strong route overrides that can bypass LLM routing after Registry access validation.
- [x] 9.5 Support weak fixed-question hints injected into LLM route context.
- [x] 9.6 Record Evidence Provider matches, evidence summaries, and provider errors in route logs.
- [x] 9.7 Document how the existing knowledge retrieval capability can be ported as the first optional plugin after the MVP interface stabilizes.

## 10. Observability, Security, And Readiness

- [x] 10.1 Implement `/ready` with `ok`, `degraded`, and `error` states for Registry and storage readiness.
- [x] 10.2 Implement route log persistence with candidate IDs, safe context summaries, evidence hints, LLM output, validation outcome, fallback decision, and timing.
- [x] 10.3 Implement run log data through Agent Run status transitions, invoker type, latency, result references, and structured errors.
- [x] 10.4 Implement admin token authentication for Registry mutation, Registry reload, and plan/control management APIs.
- [x] 10.5 Implement redaction utilities for API keys, authorization headers, secrets, internal URLs where needed, and sensitive invocation config.
- [x] 10.6 Ensure non-admin Agent and route responses never expose sensitive invocation configuration.

## 11. Tests

- [x] 11.1 Add schema validation tests for Agent Definition, RouteRequest, RouterOutput, AgentInvocation, AgentResult, plan, and artifact references.
- [x] 11.2 Add Registry tests for file loading, duplicate detection, database CRUD, hybrid fallback, degraded readiness, reload, access filtering, and sanitized views.
- [x] 11.3 Add routing tests for single intent, unsupported intent, missing required inputs, invalid LLM output fallback, target outside candidates, and `continue_agent`.
- [x] 11.4 Add route-and-invoke tests for mock, HTTP, local function, UI handoff, output validation, and invocation errors.
- [x] 11.5 Add event idempotency and Agent event callback tests.
- [x] 11.6 Add plan tests for creation, confirmation, cancellation, dependency progression, blocked state, and failure propagation.
- [x] 11.7 Add Evidence Provider tests for disabled mode, strong fixed-question override, weak hint injection, access validation, and provider failure.
- [x] 11.8 Add API integration tests using SQLite, Mock LLM, YAML sample Agents, and admin token authentication.

## 12. Documentation And Migration

- [x] 12.1 Write README with project positioning, local quickstart, configuration, API overview, and sample Agents.
- [x] 12.2 Document Agent Definition schema, invocation types, Registry backends, and file fallback semantics.
- [x] 12.3 Document Router API, route-and-invoke API, Agent event callbacks, plan APIs, and admin APIs.
- [x] 12.4 Document Evidence Provider plugin contract and fixed-question mapping format.
- [x] 12.5 Document OAC migration adapter guidance mapping `central`, `bot_id`, `route_path`, Feishu registry sync, and business Agents to generic concepts.
- [x] 12.6 Document MVP exclusions: Feishu sync, built-in Coze/Dify/FastGPT adapters, Milvus indexing, frontend UI, distributed workers, and complex session state.
- [x] 12.7 Update the requirements document or add a handoff note linking the implemented OpenSpec change artifacts.
