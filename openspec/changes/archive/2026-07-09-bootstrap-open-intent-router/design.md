## Context

`open-intent-router` is being extracted from the existing `intent_recon_sys` OAC central backend. The source system already demonstrates useful patterns: FastAPI APIs, Pydantic route schemas, candidate Agent pruning, LLM-based structured routing, conversation events, chat history, plans, route logs, and database-backed repositories.

The source system also contains coupling that must not enter the open-source core: OAC naming, private-banking examples, Feishu registry synchronization, Coze-specific fields, DeepSeek-specific configuration, Milvus/embedding knowledge modules, and business-only seed Agents.

The new project is backend-first and API-first. It must provide a generic intent router and Agent orchestration layer that can be embedded into a host application, while keeping platform-specific adapters optional.

## Goals / Non-Goals

**Goals:**

- Build a clean `open-intent-router` project structure with FastAPI runtime, typed schemas, service boundaries, repositories, invokers, and tests.
- Provide generic intent routing through `POST /api/v1/route`.
- Provide backend execution through `POST /api/v1/route-and-invoke`, limited to basic MVP invokers.
- Model Agents through generic `AgentDefinition`, `AgentInvocation`, `AgentRun`, `AgentResult`, and `AgentEvent` contracts.
- Store Agent definitions primarily in a database, with SQLite as the local default, PostgreSQL as the production-preferred database, and YAML/JSON file fallback.
- Support full Agent Registry CRUD and registry reload.
- Support OpenAI-compatible LLM routing by default, Mock LLM in tests, and DeepSeek as example configuration only.
- Preserve the useful session, event, plan, and route-log concepts from `intent_recon_sys` in generic form.
- Add an optional Evidence Provider interface for fixed-question mapping and knowledge retrieval before LLM routing.
- Provide OAC migration guidance outside the core runtime.

**Non-Goals:**

- No frontend UI or admin console in MVP.
- No Feishu registry sync in core or MVP plugins.
- No built-in Coze, Dify, FastGPT, LangGraph, or Milvus adapter in MVP.
- No SaaS multi-tenant control plane.
- No distributed worker system or long-running task queue in MVP.
- No complex session state engine; MVP reconstructs context from messages, events, results, and plans.
- No direct code mutation inside `intent_recon_sys` as part of this proposal.

## Decisions

### Decision: Build a new clean project instead of renaming the OAC backend in place

The implementation will create a clean backend package under `open_intent_router/` and use `intent_recon_sys` only as a reference.

Alternatives considered:

- Rename and strip `intent_recon_sys` in place. This would preserve history but risks leaking OAC-specific assumptions and unrelated dirty worktree state.
- Copy files wholesale and delete coupling later. This is faster initially but creates more cleanup risk.

Rationale: the open-source surface is a new product with different contracts. A clean package makes module boundaries and public APIs easier to reason about.

### Decision: Use explicit service boundaries

The runtime will be organized around:

- `api`: FastAPI routers.
- `schemas`: Pydantic request, response, and domain schemas.
- `core`: settings, errors, security, logging, and dependency wiring.
- `services`: routing, registry, context, invocation, plans, events, evidence, and logging.
- `repositories`: database, file, and memory persistence boundaries.
- `invokers`: basic Agent execution adapters.
- `llm`: OpenAI-compatible and mock LLM clients.
- `plugins`: optional extension contracts and sample Evidence Provider implementation.

Rationale: this follows the source project's useful layering while avoiding platform-specific modules.

### Decision: Make OpenAI-compatible LLM the default provider abstraction

The router will use an `LLMClient` protocol with an OpenAI-compatible implementation. DeepSeek will be documented as an example endpoint because it can be represented by base URL, API key, and model name.

Alternatives considered:

- Keep a DeepSeek-specific client as the default. This would preserve source behavior but incorrectly narrows the open-source project.
- Add many provider SDKs in MVP. This increases maintenance and test burden before the core contracts are stable.

Rationale: OpenAI-compatible APIs are broad enough for many providers and simple enough for MVP.

### Decision: Use database/file/hybrid Registry sources

Agent Registry loading will support:

- `database`: database is required.
- `file`: YAML/JSON file is required and read-only unless explicitly reloaded.
- `hybrid`: database is primary; local file is fallback when database is unavailable.

When hybrid falls back to file, readiness must report `degraded`. If the database is available but empty, file fallback only occurs when explicitly configured.

Alternatives considered:

- Always merge database and file definitions. This can accidentally enable sample Agents in production.
- Keep only database. This makes local development and degraded startup harder.

Rationale: the requirements call for database as the main source and file as a safe fallback.

### Decision: Replace `bot_id` and `route_path` with `invocation` and `ui_handoff`

Core Agent definitions will not expose OAC or Coze fields. Third-party platform identifiers belong in `invocation.config` or provider-specific metadata. Host application routing belongs in `ui_handoff`.

Rationale: this keeps the core schema stable while allowing adapters to interpret provider-specific configuration.

### Decision: Implement route-and-invoke with basic invokers only

MVP invokers will be:

- `http`
- `local_function`
- `mock`
- `ui_handoff`

Workflow and third-party platform invokers are deferred.

Rationale: route-and-invoke is required, but the MVP should prove the execution contract before adding external platform complexity.

### Decision: Run fixed-question matching before LLM routing

Evidence Providers can return fixed-question matches as either strong `route_override` or weak routing hints. Strong matches can bypass LLM routing; weak matches are injected into context.

Rationale: the existing knowledge capability is useful, but it must become optional and generic. Fixed-question mapping also directly supports deterministic routing for known questions.

### Decision: Keep session state out of MVP behavior

MVP context will be rebuilt from persisted chat messages, conversation events, recent Agent results, active plans, and artifact references. The `session_states` concept remains a future extension.

Rationale: the source system has a `session_states` table but does not clearly rely on it in the main flow. Introducing it now would add complexity without proven MVP value.

### Decision: Protect admin APIs with a simple token first

Admin Registry mutation and reload endpoints will require `ADMIN_API_TOKEN` in MVP.

Alternatives considered:

- Full OAuth/OIDC integration. This is more production-ready but too large for the initial open-source backend.
- No auth. This is unsafe because Registry definitions can contain invocation behavior.

Rationale: token auth is simple, testable, and enough to prevent accidental unauthenticated mutations.

## Risks / Trade-offs

- LLM output can be malformed or unsafe -> enforce Pydantic validation, candidate-agent post-validation, action invariants, structured fallback errors, and Mock LLM tests.
- Registry schema can become overdesigned -> keep adapter-specific fields inside `metadata`, `provider_config`, or `invocation.config`; only common fields become first-class.
- File fallback can accidentally enable sample Agents -> hybrid mode must prefer database and only use file on database unavailability unless fallback-on-empty is explicitly enabled.
- Local function invocation can become arbitrary code execution -> only allow functions registered in process by trusted code; never import user-provided paths from Agent definitions.
- HTTP invokers can leak secrets in logs -> redact headers, tokens, and invocation config in public APIs and logs.
- Route-and-invoke can scope creep into a workflow engine -> keep MVP synchronous or light async, defer distributed queues and advanced workflow engines.
- Evidence Provider can become hidden business logic -> define a narrow plugin contract and keep provider decisions visible in route logs.

## Migration Plan

1. Create the new backend project structure under `open_intent_router/`.
2. Port concepts, not platform-specific files, from `intent_recon_sys`:
   - route schemas and router service pattern
   - registry filtering pattern
   - context builder pattern
   - chat/event/plan/result repositories
   - route logging pattern
3. Implement generic schemas and settings before API routes.
4. Implement storage with SQLite first, then keep PostgreSQL compatibility through SQLAlchemy models and migrations.
5. Implement file Registry loading and hybrid fallback.
6. Implement route-only flow with Mock LLM and YAML Agents.
7. Implement OpenAI-compatible LLM route flow.
8. Implement basic invokers and `route-and-invoke`.
9. Add plan orchestration and Agent event progression.
10. Add Evidence Provider interfaces and a fixed-question provider sample.
11. Add tests and documentation.
12. Write OAC migration adapter documentation mapping old concepts to new contracts.

Rollback strategy: because this creates a new open-source project instead of modifying `intent_recon_sys`, rollback means disabling or deleting the new implementation artifacts while leaving the source system untouched.

## Open Questions

- The exact packaging name should be confirmed during implementation: `open_intent_router` for Python package and `open-intent-router` for distribution/README naming is recommended.
- The first Evidence Provider sample can be file-backed fixed-question mapping; porting the existing knowledge retrieval implementation can be scheduled after the MVP plugin interface is stable.
- The exact migration-tool choice for SQLAlchemy can be decided during implementation; Alembic is the expected default, but MVP may start with create-all for local development plus migration-ready models.
