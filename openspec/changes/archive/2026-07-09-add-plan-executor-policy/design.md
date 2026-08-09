## Context

`open-intent-router` currently supports route-only and route-and-invoke flows. Single-agent requests can be invoked by the backend through `InvocationService`, while multi-intent requests are represented as `show_plan + plan` and then rely on the UI or host application to confirm, open the current Agent, submit Agent events, and continue the next step.

That model is understandable for the original OAC-style host application, but it is not ideal for an open-source framework whose target users include enterprise chat systems, internal assistants, and applications that want a plug-and-play intent router. In those systems, the backend should own orchestration state and execution where possible, while the host handles presentation and unavoidable user or UI handoff interactions.

The current `invoke` layer is a useful building block, but it only executes one selected Agent. This change introduces a backend Plan Executor that uses `InvocationService` to execute eligible plan steps and moves plan progression out of the frontend.

## Goals / Non-Goals

**Goals:**

- Make `plan` the primary multi-intent contract instead of requiring UI clients to key off `decision.action=show_plan`.
- Preserve current `show_plan` compatibility while allowing future route outputs to use `decision.action=plan`, `reply`, or other non-UI-specific actions when a plan is present.
- Add backend execution policies for return-only, confirmation-gated, and automatic plan execution.
- Reuse existing Agent Registry and InvocationService instead of creating a second execution path.
- Keep Agent Registry changes minimal for MVP. Infer execution behavior from existing `AgentDefinition.type`, `invocation.type`, and optional metadata overrides only when necessary.
- Support host handoff pauses for UI-only Agents or steps that require user input.

**Non-Goals:**

- Do not build a full distributed workflow engine.
- Do not add durable background queues in the first implementation.
- Do not implement parallel step execution in MVP, even though the plan dependency model can allow it later.
- Do not require every Agent definition to add new execution fields.
- Do not remove the existing `show_plan` action immediately.

## Decisions

### Decision 1: Use `plan` Presence As The UI Display Signal

Clients SHALL display or inspect a plan when the route response includes `plan`, regardless of `decision.action`.

Rationale:
- `show_plan` is a UI action, not the core domain concept.
- Enterprise chat systems may want to auto-execute a plan while still showing progress.
- Host applications can choose their own UI behavior based on `plan.status` and `next_action`.

Alternative considered:
- Keep `show_plan` as the only multi-intent signal.
- Rejected because it keeps the router coupled to a specific frontend interaction model.

### Decision 2: Introduce `execution_policy`

The route or plan response SHALL include an execution policy:

- `return_plan_only`
- `require_confirmation`
- `auto_execute`
- `host_managed`

Default policy SHOULD be configurable. A conservative default for local and production is `require_confirmation`, while examples and tests can use `auto_execute` for safe mock/local Agents.

Rationale:
- Different enterprises have different risk tolerance.
- Some Agents are safe to execute automatically; others need user approval.
- A global default avoids expanding every Agent definition.

Alternative considered:
- Add mandatory execution fields to Agent Registry.
- Rejected for MVP because it increases setup burden and hurts plug-and-play adoption.

### Decision 3: Add A Backend Plan Executor

The Plan Executor SHALL:

- load the plan
- choose the next executable step
- resolve the Agent definition
- call `InvocationService`
- store run/result through the existing invocation path
- update step status
- copy or reference step outputs for dependent steps
- stop when the plan is complete, blocked, failed, cancelled, or waiting for host action

Rationale:
- Existing `InvocationService` already encapsulates Agent invocation and result persistence.
- Plan state transitions should be deterministic and testable on the backend.

Alternative considered:
- Keep host/frontend responsible for invoking each step.
- Rejected as the default because it makes every adopter reimplement orchestration.

### Decision 4: Use `next_action` For Host Collaboration

Route and plan execution responses SHALL include a `next_action` object when host participation is needed.

Examples:
- `confirm_plan`
- `open_ui`
- `collect_input`
- `wait_for_agent_event`
- `none`

Rationale:
- Host apps need explicit instructions, but those instructions should be separate from route classification.
- This avoids overloading `decision.action`.

Alternative considered:
- Encode everything in `decision.action`.
- Rejected because route decisions become an unstable mix of intent class, UI command, and executor state.

### Decision 5: Keep Registry Additions Optional

MVP SHALL infer execution mode:

- `mock`, `http`, and `local_function` are backend-invokable.
- `ui_handoff` requires host action.
- `workflow` and `provider_platform` are backend-invokable only if an invoker is registered.

Optional overrides MAY be read from `AgentDefinition.metadata.execution` without adding first-class schema fields in MVP.

Rationale:
- Avoids making simple Agent definitions verbose.
- Leaves room for future first-class fields once patterns stabilize.

Alternative considered:
- Add `execution.mode`, `side_effect`, and `requires_confirmation` to the registry schema now.
- Deferred because it is useful but not required to prove the architecture.

## Risks / Trade-offs

- Backend auto-execution may perform unwanted side effects -> default to `require_confirmation` and only auto-execute when policy allows it.
- Long-running Agents can block HTTP requests -> MVP should execute synchronously with clear timeout limits; future work can add a queue.
- Result mapping between steps can become complex -> MVP should pass prior step outputs in a simple `context.previous_results` structure.
- Host applications still need to handle UI handoff -> return explicit `next_action` and pause the plan instead of hiding this complexity.
- Keeping `show_plan` compatibility can create two mental models -> documentation should state that `plan` is the new primary contract and `show_plan` is transitional.

## Migration Plan

1. Add new schemas for `ExecutionPolicy`, `NextAction`, and plan execution responses.
2. Update prompt schema hints so multi-intent routes return `plan` and policy data; keep `show_plan` accepted.
3. Add PlanExecutor using existing PlanService and InvocationService.
4. Add plan execution endpoints while keeping current plan confirm/cancel endpoints.
5. Update visual test UI to display plan by presence and render `next_action`.
6. Add tests for return-only, confirmation-gated execution, auto execution, host handoff pause, and failure handling.
7. Later, deprecate direct reliance on `decision.action=show_plan` in docs and examples.

## Open Questions

- Should `auto_execute` be disabled by default in non-local environments?
- Should `route-and-invoke` evolve into `route-and-execute`, or should both endpoints coexist?
- Should optional registry execution overrides remain under `metadata`, or become first-class schema fields after MVP feedback?
