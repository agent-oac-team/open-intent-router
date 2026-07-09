## Why

当前路由上下文主要以历史消息、最近结果、Evidence 和前端上下文片段直接进入 `RouteContext.metadata`，缺少统一 schema、预算控制和裁剪记录。M4 需要建立 Context Pack 作为后续 Memory、Evidence、Plan 增强共同使用的入模型基础设施，避免上下文来源各自拼接、难以解释或超出模型预算。

## What Changes

- 定义 Context Pack 和 Context Item schema，用统一结构承载用户输入、当前 Agent 状态、Plan、历史消息、最近结果、事件、Evidence 和未来 Memory。
- 新增宿主可调用的会话消息写入接口，让子 Agent 回复、宿主托管 Agent 消息和必要的用户侧消息可以写成 `ChatMessage`，作为 Agent 聊天历史的事实源。
- 增加 Context Budget，默认使用 token 作为预算单位，支持模型级默认预算和请求级覆盖；场景级预算仅预留，不作为本 change 的开发要求。
- 增加字符上限和 token 预算换算能力，用于宿主侧只有字符限制或 Provider 不返回 token usage 的环境。
- 实现上下文选择、排序和裁剪：当前输入、当前任务状态和安全边界优先保留，低优先级、低相关性或超预算内容被裁剪或压缩。
- 记录 Context Pack 使用摘要、预算使用、保留/丢弃项目和裁剪原因到 Route Log / Debug 数据中。
- 让 LLM 路由输入使用 `RouteContext.metadata.context_pack` 中的 Context Pack 摘要或选中项，减少直接依赖散落在 `RouteContext.metadata` 中的原始上下文。
- 在本地测试 UI 的 Context tab 展示 Context Pack、预算使用和裁剪原因。
- M4 首版只做截断和摘要占位，不调用额外 LLM 做摘要压缩。
- 不实现 M5 Memory Store、M6 Evidence Provider 调度或完整安全脱敏模块；这些模块后续接入 Context Pack。

## Capabilities

### New Capabilities

- `context-pack-contract`: Context Pack 与 Context Item 的核心 schema、来源类型、优先级和入模型结构。
- `context-budget-control`: token/字符预算、估算、排序、裁剪和压缩降级行为。
- `context-observability`: Route Log、Debug 和测试台 Context tab 中的上下文使用摘要与裁剪可解释性。

### Modified Capabilities

- None. 当前仓库尚未归档 `openspec/specs/` 基线，本 change 以新增 M4 能力 spec 记录后续实现契约。

## Impact

- Backend schema:
  - 新增 Context Pack / Context Item / Context Budget / Context Usage 相关模型。
  - 新增或扩展会话消息写入请求模型，复用 `ChatMessage` 的 `source`、`role`、`agent_id`、`agent_session_id` 等通用字段。
  - `RouteContext.metadata` 可继续保留兼容信息，但路由内部优先使用结构化 Context Pack。
- Backend services:
  - `app/api/sessions.py`
  - `app/services/chat_history_service.py`
  - `app/services/context_service.py`
  - `app/services/router_service.py`
  - `app/llm/client.py`
  - `app/llm/mock.py`
  - `app/llm/openai_compatible.py`
  - `app/schemas/routing.py`
  - `app/schemas/logs.py`
  - `app/core/config.py`
- Frontend:
  - `web/src/types.ts`
  - `web/src/App.tsx`
  - `web/src/App.test.tsx`
  - Context tab 从占位状态升级为展示 Context Pack 和预算信息。
- Documentation:
  - `docs/api.md`
  - `docs/visual-test-ui.md`
  - 必要时更新 `docs/中控能力设计文档.md` 中 M4 已落地状态。
- Tests:
  - Context Pack 构建、预算裁剪、Route Log 摘要、LLM 输入和前端 Context tab 测试。
