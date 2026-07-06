# open-intent-router

`open-intent-router` 是一个轻量级、可插拔的意图识别与 Agent 编排后端，面向多 Agent 应用、企业内部工具、AI 工作流入口和需要统一路由层的智能应用。

它帮助宿主应用接收自然语言请求，识别用户意图，筛选可用 Agent / Tool / Workflow，返回结构化路由决策，并在需要时执行基础的后端调用。同时，项目提供会话上下文、事件、计划、执行结果和路由日志等后端能力。

## 适合解决的问题

- 将用户自然语言意图路由到多个已注册 Agent 中的一个。
- 根据角色、用户组、租户和用户属性过滤可用 Agent。
- 为宿主应用返回稳定、可审计的路由决策结构。
- 提供基础 `route-and-invoke` 能力，支持路由后直接调用目标 Agent。
- 使用数据库注册表作为主数据源，并支持 YAML / JSON 本地文件兜底。
- 通过可选 Evidence Provider 支持固定问法强路由、无权限拒绝、意图提示和证据上下文。

## 不适合解决的问题

- 不提供前端控制台或可视化搭建器。
- 不内置飞书、多维表格、OAC、银行私行业务等专有系统耦合。
- 不在 MVP 中内置 Coze、Dify、FastGPT、LangGraph 等平台适配器。
- 不替代 LangChain / LangGraph 的工作流编排能力，而是作为上游意图路由与调用入口。
- 不提供完整知识库管理、向量索引构建或分布式任务调度系统。

## MVP 范围

当前已经实现的核心能力：

- FastAPI 后端服务。
- 通用 Agent Definition Schema。
- Agent Registry 的文件、数据库、混合模式设计。
- SQLite 本地默认数据库，以及兼容 PostgreSQL 的 SQLAlchemy 模型。
- OpenAI-compatible LLM Client 和 Mock LLM。
- 基础 Invoker：`mock`、`http`、`local_function`、`ui_handoff`。
- 会话消息、Agent 事件、Agent Run / Result、Plan 和 Route Log。
- Admin Token 保护的注册表变更接口；local loopback 可选择免 token 开发。
- 可选 Evidence Provider 插件，以及文件型固定问题命中插件。
- 本地可视化测试 UI，用于配置 Agent/Intent 和对话调试。

暂未纳入 MVP 的能力：

- 飞书同步注册表。
- Coze / Dify / FastGPT / LangGraph 内置适配器。
- Milvus 索引和完整知识文件管理。
- 分布式 Worker。
- 复杂会话状态机。

## 快速开始

```bash
cd /Users/lijingtong/project/open_intent_router
python -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env
uvicorn app.main:app --reload
```

本地默认配置：

- `REGISTRY_BACKEND=file`
- `REGISTRY_FILE_PATH=./config/agents.example.yaml`
- `ROUTER_LLM_PROVIDER=mock`
- `ROUTER_PROMPT_FILE=./config/prompts/router.zh.yaml`
- `DATABASE_URL=sqlite+aiosqlite:///./data/open-intent-router.db`

启动后可访问：

- 健康检查：`GET /health`
- 就绪检查：`GET /ready`
- API 文档：`GET /docs`

## 可视化测试 UI

本地测试 UI 位于 [web](/Users/lijingtong/project/open_intent_router/web)，用于配置 Agent/Intent、切换 route-only / route-and-invoke 测试路径，并通过对话框查看 RouteResponse、InvocationResult、Evidence、UI Handoff 和 Plan。

启动前端：

```bash
cd /Users/lijingtong/project/open_intent_router/web
npm install
npm run dev
```

默认访问：

```text
http://127.0.0.1:5173
```

如果后端不是 `http://127.0.0.1:8000`，可设置：

```bash
VITE_API_PROXY_TARGET=http://127.0.0.1:8010 npm run dev
```

## 核心 API

路由与调用：

- `POST /api/v1/route`
- `POST /api/v1/route-and-invoke`
- `POST /api/v1/route-and-execute`
- `POST /api/v1/invoke`

Agent 查询：

- `GET /api/v1/agents`
- `GET /api/v1/agents/{agent_id}`
- `POST /api/v1/agents/available`

事件、执行记录与计划：

- `POST /api/v1/events/agent`
- `POST /api/v1/runs/{run_id}/events`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/plans/{plan_id}`
- `POST /api/v1/plans/{plan_id}/actions`
- `POST /api/v1/plans/{plan_id}/execute`
- `POST /api/v1/plans/{plan_id}/confirm-and-execute`
- `POST /api/v1/plans/{plan_id}/resume`
- `GET /api/v1/sessions/{session_id}/messages`

管理接口需要传入 `X-Admin-Token` 或 `Authorization: Bearer <token>`：

- `GET /api/v1/admin/agents`
- `POST /api/v1/admin/agents`
- `PUT /api/v1/admin/agents/{agent_id}`
- `PATCH /api/v1/admin/agents/{agent_id}/enabled`
- `DELETE /api/v1/admin/agents/{agent_id}`
- `POST /api/v1/admin/registry/reload`

本地开发时，如果 `APP_ENV=local` 且没有配置 `ADMIN_API_TOKEN`，Admin 写操作仅允许来自本机 loopback 访问。非 local 环境必须配置 `ADMIN_API_TOKEN`；如果 local 环境配置了 token，也需要在请求或 UI 中填写 token。

## 路由请求示例

```json
{
  "session_id": "sess_001",
  "user": {
    "id": "user_001",
    "roles": ["operator"],
    "groups": ["default"],
    "attributes": {"tenant_id": "tenant_a"}
  },
  "input": {
    "type": "text",
    "text": "帮我生成一段客户邀约话术，语气专业一点。"
  }
}
```

在默认 Mock 配置下，路由器会基于本地 `config/agents.example.yaml` 中的 Agent 定义返回路由决策。使用 `route-and-invoke` 时，如果目标 Agent 支持后端调用，会继续返回调用结果。

## Plan 与多意图

多意图的主契约是响应中的 `plan`，而不是某个 UI 专用动作。旧版 `decision.action=show_plan` 仍然兼容，但新接入方应优先判断响应中是否存在 `plan`：

- 有 `plan`：可以展示或执行多步骤任务。
- 有 `execution_policy`：后端或 Host App 根据策略决定是否自动执行、等待确认或交给宿主系统。
- 有 `next_action`：说明需要用户或 Host App 执行下一步，例如确认计划、打开页面或补充输入。

当前执行策略：

- `return_plan_only`：只返回计划，不执行。
- `require_confirmation`：需要确认后执行。
- `auto_execute`：后端自动执行可调用步骤。
- `host_managed`：Host App 自行推进。

`invoke` 仍是单 Agent 调用能力；多步骤执行由 PlanExecutor 通过 `/plans/{plan_id}/execute`、`/plans/{plan_id}/confirm-and-execute` 和 `/route-and-execute` 复用底层 invoker 完成。

## Prompt 配置

OpenAI-compatible 路由器的 Prompt 已从 LLM Client 中拆出，默认配置文件位于：

- [config/prompts/router.zh.yaml](/Users/lijingtong/project/open_intent_router/config/prompts/router.zh.yaml)

可通过环境变量指定其他 Prompt 文件：

```bash
ROUTER_PROMPT_FILE=./config/prompts/router.zh.yaml
```

Prompt 模板支持两个字段：

- `system_prompt`：系统提示词。
- `user_template`：用户消息模板，使用 `{payload_json}` 占位符注入结构化路由输入。

如果配置文件不存在，系统会回退到 [app/prompts/router_prompt.py](/Users/lijingtong/project/open_intent_router/app/prompts/router_prompt.py) 中的默认 Prompt。

## Context Pack 预算配置

M4 的默认上下文预算写在 `Settings` 里，但会被 `.env` 同名环境变量覆盖。修改后需要重启后端进程。

```dotenv
CONTEXT_DEFAULT_TOKEN_BUDGET=2000
CONTEXT_MAX_TOKEN_BUDGET=8000
CONTEXT_DEFAULT_SOURCE_BUDGETS=evidence:600,agent_history:500,host_history:400
CONTEXT_CHARS_PER_TOKEN=4
CONTEXT_PER_ITEM_TOKEN_LIMIT=512
CONTEXT_PER_ITEM_CHAR_LIMIT=2000
CONTEXT_ALLOW_REQUEST_BUDGET_OVERRIDE=true
CONTEXT_ALLOW_SUMMARY_PLACEHOLDER=true
```

- `CONTEXT_DEFAULT_TOKEN_BUDGET`：没有请求级覆盖时的总 token 预算。
- `CONTEXT_MAX_TOKEN_BUDGET`：请求级覆盖允许使用的上限。
- `CONTEXT_DEFAULT_SOURCE_BUDGETS`：可选来源预算，格式为 `source:tokens`，逗号分隔。
- `CONTEXT_CHARS_PER_TOKEN`：字符数到 token 估算的换算比例。
- `CONTEXT_PER_ITEM_TOKEN_LIMIT` / `CONTEXT_PER_ITEM_CHAR_LIMIT`：单个 Context Item 的裁剪上限。
- `CONTEXT_ALLOW_REQUEST_BUDGET_OVERRIDE`：是否允许请求或 `frontend_context` 覆盖预算。
- `CONTEXT_ALLOW_SUMMARY_PLACEHOLDER`：截断时是否标记 summary placeholder；M4 不调用额外 LLM 做摘要。

## DeepSeek 配置

DeepSeek 通过 OpenAI-compatible Provider 接入，不需要 DeepSeek 专用硬编码依赖。可复制示例：

```bash
cp .env.deepseek.example .env
```

关键字段：

```dotenv
ROUTER_LLM_PROVIDER=openai_compatible
ROUTER_LLM_MODEL=deepseek-chat
ROUTER_LLM_BASE_URL=https://api.deepseek.com
ROUTER_LLM_API_KEY=replace-with-real-key
```

原项目 DeepSeek 字段映射：

| 原项目字段 | open-intent-router 字段 |
| --- | --- |
| `DEEPSEEK_API_KEY` | `ROUTER_LLM_API_KEY` |
| `DEEPSEEK_MODEL` | `ROUTER_LLM_MODEL` |
| `DEEPSEEK_BASE_URL` | `ROUTER_LLM_BASE_URL` |

## 文档

- 需求分析：[docs/开源意图识别项目需求分析文档.md](/Users/lijingtong/project/open_intent_router/docs/开源意图识别项目需求分析文档.md)
- Agent 定义：[docs/agent-definition.md](/Users/lijingtong/project/open_intent_router/docs/agent-definition.md)
- API 概览：[docs/api.md](/Users/lijingtong/project/open_intent_router/docs/api.md)
- CI/CD 验收：[docs/ci-cd-acceptance.md](/Users/lijingtong/project/open_intent_router/docs/ci-cd-acceptance.md)
- 可视化测试 UI：[docs/visual-test-ui.md](/Users/lijingtong/project/open_intent_router/docs/visual-test-ui.md)
- Evidence Provider：[docs/evidence-provider.md](/Users/lijingtong/project/open_intent_router/docs/evidence-provider.md)
- OAC 迁移说明：[docs/oac-migration.md](/Users/lijingtong/project/open_intent_router/docs/oac-migration.md)
- 中控系统交付说明：[docs/中控系统交付说明.md](/Users/lijingtong/project/open_intent_router/docs/中控系统交付说明.md)

## CI/CD 验收

仓库已提供基础 CI 门禁和自动化巡检配置：

- `.github/workflows/ci.yml`：PR / push 到 `main`、`master`、`dev` 时运行后端 pytest、Python ruff、前端 Vitest / build 和 OpenSpec changed validation。
- `.github/workflows/inspection.yml`：每周或手动运行依赖漏洞、依赖新鲜度和后续性能基线入口。
- `.github/dependabot.yml`：为 Python、前端 npm 和 GitHub Actions 创建依赖更新 PR。

本地提交前建议运行：

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
cd web && npm ci && npm run test && npm run build
openspec validate <change-name> --strict
```

Workflow 文件只会产生 GitHub checks；要让它们真正阻止合并，需要在 GitHub repository settings 中把 `backend-regression`、`python-static-checks`、`frontend-regression`、`openspec-validation` 配置为 protected branch 的 required status checks。详细设置见 [docs/ci-cd-acceptance.md](/Users/lijingtong/project/open_intent_router/docs/ci-cd-acceptance.md)。

## 设计原则

- 核心模型保持通用，不绑定具体业务系统或第三方 Agent 平台。
- 平台相关字段放入 `invocation.config`、`provider_config` 或 `metadata`，避免污染核心 Schema。
- 数据库注册表用于生产环境，本地文件注册表用于开发、测试和故障兜底。
- 路由决策、Agent 调用和事件记录分层实现，方便后续替换 LLM、Invoker 或 Registry Source。
