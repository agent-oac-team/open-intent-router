## Why

`open-intent-router` 当前主要通过 API 和测试用例验证意图识别效果，开发者需要手写请求才能调试 Agent 候选、路由结果、Prompt 配置和 LLM Provider 行为。增加一个轻量可视化测试 UI，可以让维护者在本地快速配置意图、切换 Mock/LLM 路由模式，并通过对话框观察路由、跳转和调用结果。

DeepSeek 是原项目已验证的 OpenAI-compatible 模型来源，本项目应提供本地 `.env` 配置示例，便于从 Mock 路由切换到真实 LLM 路由，同时不把 DeepSeek 硬编码为核心依赖。

## What Changes

- 新增一个本地开发用可视化测试 UI，用于验证意图识别、Agent 路由、前端跳转交接和基础调用结果。
- UI 支持查看、创建、编辑、启用/禁用和删除 Agent/Intent 配置，底层复用现有 Admin Agent CRUD API。
- UI 支持路由模式切换：
  - 本地 Mock 回复/Mock LLM。
  - OpenAI-compatible LLM，默认可用 DeepSeek 配置示例。
  - route-only 与 route-and-invoke 测试模式。
- UI 提供对话测试框，允许输入用户消息、用户身份上下文、当前 Agent 上下文，并展示 RouteResponse、InvocationResult、Evidence 命中、Plan 和事件状态。
- 增加真实 `.env` 本地配置文件模板来源说明；仓库仍不提交真实密钥，DeepSeek API Key 使用占位符或由用户本地填写。
- 更新 README/docs，说明如何启动后端、前端测试 UI，以及如何配置 DeepSeek。

## Capabilities

### New Capabilities

- `visual-test-ui`: 本地可视化测试 UI，用于配置 Agent/Intent、对话测试、查看路由决策和调用结果。
- `runtime-routing-controls`: 开发测试场景下的路由运行时控制，包括 Mock/LLM 开关、route-only/route-and-invoke 模式和测试用户上下文。
- `deepseek-env-bootstrap`: 基于 OpenAI-compatible Provider 的 DeepSeek 本地 `.env` 配置与文档化启动流程。

### Modified Capabilities

- None.

## Impact

- 新增前端应用或静态 UI 目录，例如 `web/`、`frontend/` 或 `app/static/`，具体位置由设计阶段确定。
- 可能新增后端只读配置状态接口，用于 UI 展示当前 LLM Provider、Prompt 文件、Registry 状态和安全脱敏后的运行模式。
- 复用现有 API：
  - `POST /api/v1/route`
  - `POST /api/v1/route-and-invoke`
  - `POST /api/v1/invoke`
  - `GET/POST/PUT/PATCH/DELETE /api/v1/admin/agents`
  - `GET /ready`
  - `GET /api/v1/plans/{plan_id}`
  - `POST /api/v1/events/agent`
- 需要新增或调整配置文档：
  - `ROUTER_LLM_PROVIDER=openai_compatible`
  - `ROUTER_LLM_MODEL=deepseek-chat`
  - `ROUTER_LLM_BASE_URL=https://api.deepseek.com`
  - `ROUTER_LLM_API_KEY=<local-secret>`
  - `ROUTER_PROMPT_FILE=./config/prompts/router.zh.yaml`
- 测试影响包括前端组件测试、API 集成测试和至少一个 Mock 路由端到端对话测试。
