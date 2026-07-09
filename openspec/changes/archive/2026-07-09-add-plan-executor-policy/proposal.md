## Why

The project is positioned as a plug-and-play intent recognition and Agent orchestration framework for enterprise applications and chat systems. The current multi-intent behavior still depends too heavily on UI-oriented `show_plan` semantics and host-side step progression, which is less suitable for teams that expect the backend to own orchestration state and execution.

## What Changes

- Introduce a backend Plan Execution Policy model that separates multi-intent planning from UI display actions.
- Treat `plan` as the primary multi-intent contract. UI clients can display a plan whenever one is returned; they should not need a special `show_plan` action to infer display behavior.
- Add a backend Plan Executor that can execute invokable plan steps by reusing the existing `InvocationService`.
- Support three execution modes:
  - `return_plan_only`: return the plan without execution.
  - `require_confirmation`: wait for user or host confirmation, then execute eligible steps.
  - `auto_execute`: execute eligible steps immediately after routing.
- Keep host participation for UI handoff, human input, confirmation, and display, but move step selection, invocation, result persistence, and plan state progression into the backend.
- Keep Agent Registry changes minimal. Do not require broad new registry fields for MVP. Use global defaults and infer execution behavior from existing `AgentDefinition.type` and `invocation.type`.
- Preserve compatibility with the current `show_plan` action during transition, but stop treating it as the core multi-intent mechanism.

## Capabilities

### New Capabilities

- `plan-execution-policy`: Defines how routed plans choose between return-only, confirmation-gated, and automatic execution.
- `plan-executor`: Defines backend execution of plan steps, including invocation, result capture, status updates, dependency handling, and host handoff pauses.
- `route-plan-contract`: Defines the route response contract where returned `plan` and `next_action` drive UI behavior instead of requiring a `show_plan` action.

### Modified Capabilities

- None. There is no archived baseline `openspec/specs/` directory yet; this change introduces new capability specs while preserving current API compatibility during migration.

## Impact

- Backend services:
  - `RouterService`
  - `PlanService`
  - `InvocationService`
  - new `PlanExecutor` service
- Schemas:
  - route response / route-and-invoke response
  - plan and plan step metadata
  - execution policy and next action models
- APIs:
  - add confirm-and-execute / execute / resume plan endpoints
  - keep existing confirm/cancel endpoints for compatibility
  - optionally add route-and-execute endpoint
- Frontend test UI:
  - display plan whenever response includes `plan`
  - show `next_action`
  - no longer rely on `decision.action=show_plan` as the only plan-display signal
- Agent Registry:
  - no required MVP field expansion
  - optional minimal override may be added only if implementation needs it, such as `metadata.execution_policy`
