## Context

`open-intent-router` 已具备后端 API、Agent Registry、Mock/OpenAI-compatible LLM 路由、Prompt 文件配置、基础 Invoker、Plan 和事件能力。当前缺口是缺少一个给开发者和维护者使用的可视化测试入口：用户需要通过 curl、pytest 或手写 JSON 来验证意图识别效果，调试成本较高。

原项目使用 DeepSeek 作为 LLM 路由模型，配置字段为 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL=deepseek-chat`、`DEEPSEEK_BASE_URL=https://api.deepseek.com`。开源项目已经抽象为 `ROUTER_LLM_PROVIDER=openai_compatible`、`ROUTER_LLM_MODEL`、`ROUTER_LLM_BASE_URL`、`ROUTER_LLM_API_KEY`，因此 DeepSeek 应作为本地 `.env` 示例和文档化配置，而不是新增专用硬编码 Provider。

## Goals / Non-Goals

**Goals:**

- 提供一个本地可视化测试 UI，用于配置 Agent/Intent、发起对话测试、查看路由和调用结果。
- 支持测试者在 UI 中选择 Mock 路由或真实 OpenAI-compatible LLM 路由，并明确展示当前运行模式。
- 支持 route-only 和 route-and-invoke 两种测试路径。
- 支持配置测试用户上下文、当前 Agent 上下文、session_id、source 等关键 RouteRequest 字段。
- 复用现有 Agent Admin CRUD API，不新建独立的 Intent 存储模型。
- 提供 `.env` 文件生成/复制流程和 DeepSeek 示例配置，保持真实 API Key 不入库。
- 保持开源核心不绑定 DeepSeek、OAC、飞书或银行业务。

**Non-Goals:**

- 不做生产级管理控制台、权限系统或多租户后台。
- 不实现复杂前端用户登录。
- 不把 LLM Provider 切换设计成运行时全局热切换；MVP 可通过 `.env` 和服务重启生效。
- 不在前端保存真实 API Key。
- 不实现可视化 Prompt 编辑器；Prompt 文件编辑仍通过本地文件完成。
- 不引入 Coze/Dify/FastGPT/LangGraph 专用适配器。

## Decisions

### Decision 1: 使用独立前端开发应用

在项目中新增独立前端目录，例如 `web/`，使用 Vite + React + TypeScript 构建本地测试 UI。

理由：

- 前端交互复杂度已经超过静态 HTML 表单，React 适合管理对话消息、Agent 编辑表单、路由结果面板和状态切换。
- Vite 启动快，适合本地开发工具。
- 独立目录可以避免污染 FastAPI 后端模块边界。

备选方案：

- FastAPI Jinja2 模板：依赖少，但复杂交互和状态管理会变差。
- 纯静态 HTML：实现简单，但 Agent CRUD 表单、JSON 编辑器和对话状态维护会很脆。

### Decision 2: UI 通过后端现有 API 操作 Agent/Intent

“配置意图”在 MVP 中映射为配置 Agent Definition 的 `description`、`capabilities`、`trigger`、`required_inputs`、`access_policy` 和 `invocation`，不新增单独 Intent 表。

理由：

- 当前路由器的意图识别对象是候选 Agent，Agent Definition 已经承载触发词、正反例、能力描述和调用协议。
- 避免在 MVP 中引入 Intent 与 Agent 的二级映射复杂度。
- 后续如果需要“一意图多 Agent”或固定问映射，可扩展 Evidence Provider 或新增 Intent Profile。

### Decision 3: Mock/LLM 开关优先使用配置状态展示与请求级测试模式

后端全局 LLM Provider 仍由 `.env` 控制；UI 展示当前 provider、model、prompt file、registry source 等脱敏配置。UI 中的“本地回复/大模型”开关用于构造测试请求和提示用户当前服务是否匹配，MVP 不强制实现运行时修改 `.env` 并热重载。

理由：

- 运行时修改全局 LLM Provider 涉及并发请求一致性、安全审计和客户端生命周期，超出 MVP。
- 当前 Settings 使用 `.env` 加载，重启后生效更简单稳定。

可选增强：

- 后续增加 `POST /api/v1/admin/runtime/router-mode`，仅在 `APP_ENV=local` 下可用。

### Decision 4: DeepSeek 通过 OpenAI-compatible 配置接入

新增本地 `.env` 示例或生成说明：

```dotenv
ROUTER_LLM_PROVIDER=openai_compatible
ROUTER_LLM_MODEL=deepseek-chat
ROUTER_LLM_BASE_URL=https://api.deepseek.com
ROUTER_LLM_API_KEY=replace-with-real-key
```

理由：

- DeepSeek API 可通过 OpenAI-compatible Chat Completions 调用。
- 保持核心 Provider 抽象通用，不新增 DeepSeek 专用依赖。
- 与原项目字段可文档化映射：`DEEPSEEK_MODEL` → `ROUTER_LLM_MODEL`，`DEEPSEEK_BASE_URL` → `ROUTER_LLM_BASE_URL`，`DEEPSEEK_API_KEY` → `ROUTER_LLM_API_KEY`。

### Decision 5: 新增脱敏运行时状态接口

新增只读接口，例如 `GET /api/v1/admin/runtime/config` 或 `GET /api/v1/runtime/config`，返回 UI 所需运行状态：

- `router_llm_provider`
- `router_llm_model`
- `router_llm_base_url`
- `router_prompt_file`
- `registry_backend`
- `storage_backend`
- `evidence_provider_enabled`
- `route_mode`

敏感字段必须脱敏或不返回，尤其是 API Key、Admin Token、数据库密码。

## Risks / Trade-offs

- [Risk] 前端依赖会增加项目安装复杂度 → Mitigation: 将前端依赖限制在 `web/`，后端仍可独立运行；README 区分后端启动和 UI 启动。
- [Risk] 用户误以为 UI 是生产控制台 → Mitigation: 页面标题、README 和 docs 明确标注为 Local Test UI / Dev Console。
- [Risk] 前端展示配置时泄露密钥 → Mitigation: 运行时状态接口不返回 secret 值，只返回 provider、model、base_url 和布尔状态。
- [Risk] Mock/LLM 切换期待运行时立即生效 → Mitigation: MVP 文档明确 `.env` 修改需要重启后端；UI 显示当前实际 provider。
- [Risk] Agent CRUD 破坏本地文件注册表语义 → Mitigation: UI 在 `REGISTRY_BACKEND=file` 时禁用写操作或提示切换到 database/hybrid 的数据库可写模式。

## Migration Plan

1. 保持现有后端 API 兼容。
2. 新增前端目录和本地启动脚本，不改变后端启动流程。
3. 新增脱敏运行时状态接口，供 UI 首屏读取。
4. 更新 `.env.example` 和新增 `.env` 本地模板说明，提供 DeepSeek OpenAI-compatible 配置。
5. 增加 README/docs 的启动步骤。
6. 如需回滚，可删除 `web/` 目录和新增运行时状态接口，现有路由和 Agent API 不受影响。

## Open Questions

- 前端目录最终命名使用 `web/` 还是 `frontend/`。
- 是否需要在 MVP 中提交真实 `.env` 文件。建议不提交真实 `.env`，只保留 `.env.example` 和 `.env.deepseek.example`，避免密钥泄露。
- UI 是否需要内置 JSON Schema 表单编辑器，还是 MVP 先使用 JSON textarea 编辑 `input_schema`、`output_schema` 和 `invocation.config`。
