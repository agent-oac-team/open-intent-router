# open-intent-router

`open-intent-router` 是一个轻量级、可插拔的意图识别与 Agent 编排后端，面向多 Agent 应用、企业内部工具、AI 工作流入口和需要统一路由层的智能应用。

它帮助宿主应用接收自然语言请求，识别用户意图，筛选可用 Agent / Tool / Workflow，返回结构化路由决策，并在需要时执行后端调用。同时，项目提供受治理上下文、Canonical Turn、事件、计划、执行结果、长期记忆、知识检索和路由日志等能力。

## 适合解决的问题

- 将用户自然语言意图路由到多个已注册 Agent 中的一个。
- 根据角色、用户组、租户和用户属性过滤可用 Agent。
- 为宿主应用返回稳定、可审计的路由决策结构。
- 提供基础 `route-and-invoke` 能力，支持路由后直接调用目标 Agent。
- 使用数据库注册表作为主数据源，并支持 YAML / JSON 本地文件兜底。
- 通过可选 Evidence Provider 支持固定问法强路由、无权限拒绝、意图提示和证据上下文。

## 不适合解决的问题

- 不提供生产级前端控制台或可视化搭建器；`web/` 仅是本地开发测试台。
- OIR Core 不内置飞书、多维表格、OAC 或银行业务耦合；OAC 专有契约隔离在 Host Adapter。
- Core 不内置 Coze、Dify、FastGPT、LangGraph 等平台适配器。
- 不替代 LangChain / LangGraph 的工作流编排能力，而是作为上游意图路由与调用入口。
- 不提供通用知识库 SaaS 控制面或分布式任务调度平台；当前知识资产能力服务于 OIR 治理与 OAC 兼容接入。

## 当前能力

当前已经实现：

- FastAPI 后端服务。
- 通用 Agent Definition Schema。
- Agent Registry 的文件、数据库、混合模式设计。
- SQLite 本地默认数据库，以及兼容 PostgreSQL 的 SQLAlchemy 模型。
- OpenAI-compatible LLM Client 和 Mock LLM。
- 受版本约束的 Runtime Catalog 与 v2 Definition Handling（`invocation`、`external_execution`、`ui_handoff`）。
- 会话消息、Agent 事件、Agent Run / Result、Plan 和 Route Log。
- Governed Context Pipeline，以及 Router / Agent 的预算、投影和 Trace。
- Canonical Turn、Transactional Outbox、Delegated Run 和幂等收口。
- PostgreSQL canonical memory lifecycle、自动 Formation 和可重建的 mem0 / Milvus 派生索引。
- PostgreSQL canonical knowledge asset / chunk / import job，以及受治理检索和 Milvus 派生索引。
- Admin Token 保护的注册表变更接口；local loopback 可选择免 token 开发。
- 可选 Evidence Provider 插件，以及文件型固定问题命中插件。
- 本地可视化测试 UI，用于配置 Agent/Intent 和对话调试。
- 同仓 OAC Host Runtime，提供 IRS 兼容 API、`OIR-HOST-V2` 身份、Execution Ticket、Shadow、Fallback 和 Cutover 治理。

当前明确不提供：

- 飞书同步注册表。
- OIR Core 内置的 Coze / Dify / FastGPT / LangGraph Adapter。
- 通用低代码搭建器、生产管理控制台或完整知识平台。
- 分布式任务 Worker / 调度集群。
- 复杂会话状态机。

## 快速开始

```bash
cd open_intent_router
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

本地测试 UI 位于 [web](web)，用于配置 Agent/Intent、切换 route-only / route-and-invoke 测试路径，并通过对话框查看 RouteResponse、InvocationResult、Evidence、UI Handoff 和 Plan。

启动前端：

```bash
cd web
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

Native 个性化接口使用 `X-OIR-Principal-Envelope`；非 local 环境还必须携带受信网关生成的
`X-OIR-Principal-Signature`。公开脱敏 Agent Catalog 仍可匿名读取。外部 Agent Event 不使用
Principal，而必须携带绑定具体 Delegated Run 的 `X-OIR-Execution-Ticket`；项目不暴露 Native
Ticket 签发 API。完整所有权、Candidate Set、Plan 取消和 deadline 语义见
[API 概览](docs/App-Desc/contracts/api.md)。

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

在默认 Mock 配置下，路由器会基于本地 `config/agents.example.yaml` 中的 Agent Definition 返回路由决策。示例中的
`invocation` Binding 还需要由部署注册同名 Runtime Adapter；缺失时 Snapshot 会安全隔离该 Definition，
不会创建临时 Client、猜测旧执行类型或回退执行。使用 `route-and-invoke` 时，只有已通过发布门禁的目标
Binding 才会继续返回调用结果。

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

`invoke` 仍是单 Agent 调用能力；多步骤执行由 PlanExecutor 通过 `/plans/{plan_id}/execute`、`/plans/{plan_id}/confirm-and-execute` 和 `/route-and-execute` 复用底层 Invocation Runtime 完成。

## Prompt 配置

OpenAI-compatible 路由器的 Prompt 已从 LLM Client 中拆出，默认配置文件位于：

- [config/prompts/router.zh.yaml](config/prompts/router.zh.yaml)

可通过环境变量指定其他 Prompt 文件：

```bash
ROUTER_PROMPT_FILE=./config/prompts/router.zh.yaml
```

Prompt 模板支持两个字段：

- `system_prompt`：系统提示词。
- `user_template`：用户消息模板，使用 `{payload_json}` 占位符注入结构化路由输入。

如果配置文件不存在，系统会回退到 [app/prompts/router_prompt.py](app/prompts/router_prompt.py) 中的默认 Prompt。

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
CONTEXT_PIPELINE_MODE=legacy
MEMORY_MODE=off
CONTEXT_ROUTE_KNOWLEDGE_ENABLED=false
CONTEXT_POLICY_VERSION=context-policy-v1
CONTEXT_BUDGET_VERSION=context-budget-v1
CONTEXT_PROJECTION_VERSION=context-projection-v1
```

- `CONTEXT_DEFAULT_TOKEN_BUDGET`：没有请求级覆盖时的总 token 预算。
- `CONTEXT_MAX_TOKEN_BUDGET`：请求级覆盖允许使用的上限。
- `CONTEXT_DEFAULT_SOURCE_BUDGETS`：可选来源预算，格式为 `source:tokens`，逗号分隔。
- `CONTEXT_CHARS_PER_TOKEN`：字符数到 token 估算的换算比例。
- `CONTEXT_PER_ITEM_TOKEN_LIMIT` / `CONTEXT_PER_ITEM_CHAR_LIMIT`：单个 Context Item 的裁剪上限。
- `CONTEXT_ALLOW_REQUEST_BUDGET_OVERRIDE`：是否允许请求或 `frontend_context` 覆盖预算。
- `CONTEXT_ALLOW_SUMMARY_PLACEHOLDER`：截断时是否标记 summary placeholder；M4 不调用额外 LLM 做摘要。
- `CONTEXT_PIPELINE_MODE`：`legacy` 保持旧 Prompt，`observe` 只构建和比较新 Projection，`enforced` 只使用 governed Projection。
- `MEMORY_MODE`：唯一记忆行为开关；`on` 固定启用 Governed route Memory，并只召回 `user_preference`、`stable_fact`。
- `CONTEXT_ROUTE_KNOWLEDGE_ENABLED`：是否启用 Router 阶段 Knowledge 检索。
- version 字段会进入 Context Trace 和 Route Log，用于后续比较与回放。

Context Pipeline 将 Candidate、Pack、Projection 和 Trace 分离。Router Prompt 在 `enforced` 模式不读取完整请求 metadata、Debug Pack 或 dropped items；`MEMORY_MODE=on` 即使在 `CONTEXT_PIPELINE_MODE=legacy` 下也会对 Memory 路径强制使用 Governed Context。Agent 仍通过稳定的 `memory_context`、`knowledge_context` 和 `RouteResponse.invocation.input` 接收上下文。

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

- [领域语言](CONTEXT.md)：OIR、Host、路由、运行事实、Context、Memory、Knowledge 和迁移治理的稳定术语。
- [文档中心](docs/README.md)：全部文档的权威级别、阅读路径和维护规则。
- [App-Desc 应用地图](docs/App-Desc/README.md)：模块、运行入口、依赖方向、事实源和治理区域。
- [App-Adr 应用规约](docs/App-Adr/README.md)：研发 / 测试标准、作用域约束和技能目录。
- [App-Research 研究索引](docs/App-Research/README.md)：需求、设计、会议和历史验收材料。
- [API 概览](docs/App-Desc/contracts/api.md) 与 [Agent 定义](docs/App-Desc/contracts/agent-definition.md)：Native 契约快速入口。

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

Workflow 文件只会产生 GitHub checks；要让它们真正阻止合并，需要在 GitHub repository settings 中把 `backend-regression`、`python-static-checks`、`frontend-regression`、`openspec-validation` 配置为 protected branch 的 required status checks。详细设置见 [docs/App-Adr/test/test-standards/ci-cd-acceptance.md](docs/App-Adr/test/test-standards/ci-cd-acceptance.md)。

## 设计原则

- 核心模型保持通用，不绑定具体业务系统或第三方 Agent 平台。
- Native Definition 只保存逻辑 Binding 引用和受限 Handling 配置；平台私有字段留在 Runtime Adapter 或 Host Adapter。
- 数据库注册表用于生产环境，本地文件注册表用于开发、测试和故障兜底。
- 路由决策、Agent 调用和事件记录分层实现，方便后续替换 LLM、Runtime Adapter 或 Registry Source。
