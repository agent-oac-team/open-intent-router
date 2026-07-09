## Why

当前路由协议已经具备基础动作、Plan 和必填输入校验，但用户可见回答、计划展示判断、候选 Agent 粗筛和槽位澄清仍混在已有字段和实现细节中。M3 需要先把这些意图层契约收拢清楚，为后续 Context Pack、Memory 和 Evidence 调度提供稳定入口。

## What Changes

- 新增顶层 `assistant_message`，作为聊天窗口唯一主来源；旧客户端可继续回退到 `decision.message`。
- 收窄现有 message 字段职责：`decision.message` 保留为路由阶段兼容文案，`next_action.message` 只描述 Plan / Host 协作提示，`AgentInvocationResult.message` 只描述执行摘要，`decision.reason` 只用于 Route / Debug。
- 迁移多意图 Plan 主契约：客户端和测试台以 `plan != null` 判断是否展示计划，服务端兜底生成 Plan 时不再强制把 `decision.action` 改写为 `show_plan`。
- 强化路由动作全集和状态校验，覆盖 `reply`、`clarify`、`open_agent`、`continue_agent`、`exit_agent`、`show_plan`、`unsupported`、`silent`。
- 增加首版标签候选筛选：在权限和可用性过滤之后，根据 Agent 配置、trigger、capabilities 或 metadata 中的统一标签约定收窄传给 LLM / Evidence Provider 的候选集；无标签命中时 fallback 到全部用户可用 Agent，并记录 `tag_filter=no_match_fallback_all_available`。
- 增强槽位澄清：目标 Agent 缺少必填输入时返回 `clarify` 或 `next_action=collect_input`，不直接调用 Agent。
- 增加保守的低置信度澄清阈值配置，低于阈值时返回澄清并记录原因。
- 保持 M3 不实现 M4 Context Pack；M3 仍使用现有上下文输入，并在设计中预留未来由 Context Pack 接管组装的接口边界。

## Capabilities

### New Capabilities

- `assistant-message-contract`: 统一用户可见回答字段，以及现有 message 字段在聊天区、Plan 区、Result 区和 Debug 区的职责边界。
- `plan-contract-migration`: 多意图计划以 `plan` 作为主契约，`show_plan` 仅保留兼容语义。
- `intent-slots-routing`: 路由动作全集、标签候选筛选、槽位缺失澄清和 route-only / route-and-invoke / route-and-execute 行为一致性。

### Modified Capabilities

- None. 当前仓库尚未归档 `openspec/specs/` 基线，本 change 以新增 M3 能力 spec 记录后续实现契约。

## Impact

- Backend API:
  - `RouteResponse` 增加可选顶层 `assistant_message: string | null`。
  - 现有 `decision.message`、`next_action.message`、`AgentInvocationResult.message` 和 `decision.reason` 保持兼容，但职责收窄。
- Backend services:
  - `app/services/router_service.py`
  - `app/services/registry_service.py`
  - `app/services/context_service.py`
  - `app/llm/mock.py`
  - `app/llm/openai_compatible.py`
  - `config/prompts/router.zh.yaml`
  - `app/prompts/router_prompt.py`
- Frontend:
  - `web/src/App.tsx`
  - `web/src/types.ts`
  - `web/src/App.test.tsx`
  - 聊天区优先展示 `assistant_message`，缺失时兼容回退到 `decision.message`；Plan 展示继续以 `plan` 存在为准。
- Documentation:
  - `docs/api.md`
  - `docs/visual-test-ui.md`
  - 必要时更新 `docs/中控能力设计文档.md` 中 M3 已落地状态。
- Tests:
  - Router、Mock LLM、OpenAI-compatible 解析、Prompt、前端聊天气泡和 Plan 展示测试。
