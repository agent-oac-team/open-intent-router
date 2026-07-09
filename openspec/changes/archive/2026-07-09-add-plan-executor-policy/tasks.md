## 1. Schemas And Configuration

- [x] 1.1 Add `ExecutionPolicy` literals: `return_plan_only`, `require_confirmation`, `auto_execute`, and `host_managed`.
- [x] 1.2 Add a `NextAction` schema with minimal fields: `type`, `message`, `agent_id`, `plan_id`, `step_id`, `route`, `params`, and `metadata`.
- [x] 1.3 Add plan execution response schemas for route-and-execute and plan execution endpoints.
- [x] 1.4 Add global settings for default plan execution policy and local-development auto-execution behavior.
- [x] 1.5 Keep Agent Registry schema unchanged for MVP; optionally read `metadata.execution_policy` if present.

## 2. Route Contract Updates

- [x] 2.1 Update route prompt schema hints so multi-intent output is driven by `plan`, `execution_policy`, and `next_action`, not only `show_plan`.
- [x] 2.2 Update route normalization so a response with `plan` is valid even when `decision.action` is not `show_plan`.
- [x] 2.3 Preserve compatibility for existing `decision.action=show_plan` responses.
- [x] 2.4 Ensure invalid multi-task outputs without a valid plan return a structured routing error or clarification.
- [x] 2.5 Update route logs to include execution policy and next action when available.

## 3. Plan Executor

- [x] 3.1 Implement a `PlanExecutor` service that depends on `PlanService`, `AgentRegistryService`, and `InvocationService`.
- [x] 3.2 Implement next-step selection using existing `current_step_id`, step status, and `depends_on`.
- [x] 3.3 Invoke backend-invokable Agents through `InvocationService`.
- [x] 3.4 Update step and plan status after completed, failed, blocked, or cancelled invocation outcomes.
- [x] 3.5 Pass previous step outputs to dependent steps through execution context.
- [x] 3.6 Pause with `next_action=open_ui` for `ui_handoff` Agents.
- [x] 3.7 Pause with `next_action=collect_input` when required inputs cannot be built.
- [x] 3.8 Stop execution when the plan reaches `completed`, `failed`, `blocked`, or `cancelled`.

## 4. API Endpoints

- [x] 4.1 Add `POST /api/v1/plans/{plan_id}/execute` to execute or continue eligible plan steps.
- [x] 4.2 Add `POST /api/v1/plans/{plan_id}/confirm-and-execute` to confirm and start backend execution.
- [x] 4.3 Add `POST /api/v1/plans/{plan_id}/resume` for blocked or host-paused plans when the host provides required data.
- [x] 4.4 Add `POST /api/v1/route-and-execute` for route plus policy-driven execution.
- [x] 4.5 Keep existing `POST /api/v1/plans/{plan_id}/actions` confirm/cancel behavior for compatibility.

## 5. Invocation And Result Mapping

- [x] 5.1 Refactor shared input-building logic so single-Agent invocation and plan-step invocation use the same behavior.
- [x] 5.2 Store plan execution results with plan_id and step_id where repositories already support those fields.
- [x] 5.3 Include prior step outputs in `AgentInvocation.context.previous_results`.
- [x] 5.4 Ensure failed invocation results produce structured plan failure or blocked states.

## 6. Visual Test UI

- [x] 6.1 Display a plan whenever a route response includes `plan`, regardless of `decision.action`.
- [x] 6.2 Display `execution_policy` and `next_action` in the route result panel.
- [x] 6.3 Add controls for confirm-and-execute, execute/continue, cancel, and refresh plan.
- [x] 6.4 Show host-paused states such as open UI or collect input without requiring developers to inspect raw JSON.

## 7. Tests

- [x] 7.1 Add backend tests for route responses with `plan` and non-`show_plan` actions.
- [x] 7.2 Add backend tests for `return_plan_only`.
- [x] 7.3 Add backend tests for `require_confirmation` and confirm-and-execute.
- [x] 7.4 Add backend tests for `auto_execute` with mock Agents.
- [x] 7.5 Add backend tests for dependency ordering and previous result context.
- [x] 7.6 Add backend tests for `ui_handoff` pause and `collect_input` pause.
- [x] 7.7 Add frontend tests for plan display by presence and next action rendering.

## 8. Documentation

- [x] 8.1 Update README to describe the new plan contract and execution policies in Chinese.
- [x] 8.2 Update `docs/api.md` with new route-and-execute and plan execution endpoints.
- [x] 8.3 Update `docs/visual-test-ui.md` with plan execution controls.
- [x] 8.4 Document that `show_plan` is compatibility behavior and `plan` is the primary multi-intent contract.
