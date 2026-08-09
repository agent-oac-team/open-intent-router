# AGENTS.md

本文件是给 Codex / coding agent 使用的仓库级工作指令，作用范围为整个 `open-intent-router` 项目。

## 项目背景

`open-intent-router` 是一个面向企业应用和聊天系统的轻量级意图识别与 Agent 编排后端。项目目标是接收自然语言输入，识别用户意图，筛选可用 Agent / Tool / Workflow，返回稳定的结构化路由结果，并在安全可控的情况下执行后端可调用能力。

`app/` Core 必须保持通用，不绑定 OAC、银行业务、飞书、Coze、Dify、FastGPT 或任何单一宿主系统。OAC / IRS 专有语义只允许出现在 `host_adapters/oac`、`host_apps/oac`、兼容 fixture 和迁移材料中；业务示例不能进入核心逻辑。

## Harness 与文档路由

- 稳定领域语言：`CONTEXT.md`；需求、设计和命名出现术语冲突时，以该词汇表为准并先纠正用词。
- 文档统一入口：`docs/README.md`。
- 跨模块或不熟悉目录时先读 `docs/App-Desc/README.md`。
- 研发 / 测试规约与技能目录见 `docs/App-Adr/README.md`。
- 复杂问题和历史决策按需从 `docs/App-Research/README.md` 选择，不要全量加载。
- `app/`、`host_adapters/oac/`、`host_apps/oac/`、`tests/`、`web/` 各有作用域化 `AGENTS.md`；修改文件前必须读取路径上最近的规则。

## 常用命令

后端测试：

```bash
.venv/bin/python -m pytest
```

前端测试：

```bash
cd web
npm run test
```

前端构建：

```bash
cd web
npm run build
```

Python 静态检查：

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

OpenSpec 校验：

```bash
openspec validate <change-name> --strict
```

后端本地启动：

```bash
uvicorn app.main:app --reload
```

前端本地启动：

```bash
cd web
npm run dev
```

## 架构边界

- `app/api`：FastAPI HTTP 接口，只做协议适配、依赖注入和错误转换。
- `app/schemas`：Pydantic 请求、响应和领域模型，API 契约变更应先从这里明确。
- `app/application`：供 Host Adapter 调用的通用应用端口，禁止宿主专有类型。
- `app/services/router_service.py`：意图识别、候选 Agent 裁剪、路由决策和 Plan 生成。
- `app/services/invocation_service.py`：单 Agent 调用执行，是 PlanExecutor 复用的底层能力。
- `app/services/plan_service.py`：Plan 保存、确认、取消和事件驱动状态更新。
- `app/services/plan_executor.py`：Plan 步骤执行、依赖推进、结果回填、暂停恢复和 `next_action` 协作。
- `app/services/registry_service.py`：Agent Registry 加载、合并和候选过滤。
- `app/llm`：Mock 与 OpenAI-compatible LLM Client。
- `app/invokers`：Agent 调用器实现。
- `app/plugins/evidence.py`：可选 Evidence Provider，只提供证据、弱意图提示或固定问命中。
- `host_adapters/oac`：IRS 兼容协议、V2 身份、映射、Fallback、Shadow 和 Cutover，只能调用应用端口。
- `host_apps/oac`：OAC Host 组合根、配置和启动门禁，不复制领域状态机。
- `config/prompts/router.zh.yaml`：默认中文路由 Prompt 模板。
- `web`：本地可视化测试 UI，不是核心 Host App 实现。
- `openspec/changes`：变更提案、设计、规格和任务。

## 设计原则

- Plan 是多意图主契约。响应中出现 `plan` 即表示存在可展示或可执行的多步骤任务，不应依赖 `decision.action=show_plan` 才展示计划。
- `show_plan` 只保留为兼容语义，不应成为后续核心设计。
- 后端负责意图识别、候选裁剪、路由决策、Plan 状态、Run / Result / Event 和可后端执行的步骤推进。
- 前端或 Host App 负责展示、确认、用户输入、打开 UI 和上报外部事件。
- Agent Registry 保持轻量。MVP 不强制增加大量执行字段，优先根据 `type` / `invocation.type` 推断是否可后端执行。
- 如需覆盖执行策略，优先使用 `metadata.execution.policy` 或 `metadata.execution_policy`，待模式稳定后再考虑一等字段。
- LLM Provider、Evidence Provider、Agent Invoker、Registry Source 都应可插拔。
- 核心项目不内置重型工作流引擎、完整知识库平台或分布式任务系统。

## 开发规范

- 修改前先阅读相关 schema、service、repository 和测试，遵循当前分层。
- 保持改动范围最小，不做与任务无关的重构、格式化或依赖升级。
- 新增或修改 API 契约时，同步更新 schema、测试和 `docs/App-Desc/contracts/api.md`。
- 新增执行路径时，优先复用 `InvocationService` 和已有 invoker，不创建第二套调用逻辑。
- 新增 Agent 类型时，应同时考虑 Registry schema、invoker 注册、输入构造、输出校验和测试。
- 不把业务专有名称写入核心模块、默认 Prompt 或公共 schema。
- 数据库和本地文件注册表要保持共存：数据库为主，本地文件用于开发和兜底。
- 保持 API 向后兼容；需要破坏性迁移时先通过 OpenSpec 说明。

## 测试要求

- 后端逻辑变更至少运行 `.venv/bin/python -m pytest`。
- Python 代码或配置变更至少运行 `.venv/bin/python -m ruff check .` 和 `.venv/bin/python -m ruff format --check .`。
- 前端 UI 或类型变更至少运行 `cd web && npm run test` 和 `cd web && npm run build`。
- Plan、Router、Invocation、Registry、Admin Security 相关变更需要补充或更新对应测试。
- CI/CD、依赖或 workflow 变更需要确认 `.github/workflows/ci.yml` 中的 `backend-regression`、`python-static-checks`、`frontend-regression`、`openspec-validation` 仍然能在干净环境运行。
- 如果无法运行某项测试，最终回复中必须说明原因和风险。

## CI/CD 门禁与巡检

- PR blocking checks 只放稳定、可复现的检查：后端 pytest、ruff lint / format check、前端 npm ci / test / build、OpenSpec changed validation。
- Python CI 必须通过已提交的 `uv.lock` 做 locked install；依赖升级只在独立升级 PR 或 advisory freshness 检查中发生，普通 PR 不解析未锁定的新版本。
- 自动化巡检分层处理：secret scanning 使用 GitHub 原生能力，依赖漏洞和依赖新鲜度首版作为 scheduled / advisory，不默认阻塞普通 PR。
- CI 不依赖 `.env`、`.venv`、`web/node_modules`、真实 LLM 凭证、真实外部系统或本地数据库；需要外部服务的集成检查必须单独设计 protected environment。
- PostgreSQL 条件测试通过手动 `Release Preflight` 在隔离 service container 中运行，不纳入普通 PR required checks。
- Workflow 文件本身不会阻止合并；仓库管理员必须在 GitHub branch protection 中把稳定 job 配置为 required status checks。
- 详细操作和分层说明见 `docs/App-Adr/test/test-standards/ci-cd-acceptance.md`。

## 安全与配置

- 不要提交 `.env`、API Key、Token、数据库密码或任何真实凭证。
- `.env.example` 和 `.env.deepseek.example` 只能包含占位值。
- `ADMIN_API_TOKEN` 的策略：本地 `APP_ENV=local` 且未配置 token 时，只允许 loopback Admin 写操作；非 local 环境必须配置 token。
- 不要在日志、测试快照或文档中暴露真实 DeepSeek / OpenAI-compatible API Key。

## 文档要求

- README 和 `docs/` 下关键文档默认使用中文。
- 面向开发者和维护者写文档，避免业务汇报式表述。
- 新文档必须在 `docs/README.md` 登记状态；历史方案不得伪装成当前操作说明。
- 涉及多意图时，明确说明 `plan` 是主契约，`next_action` 是 Host 协作指令，`show_plan` 是兼容行为。
- 涉及前端测试台时，说明它用于本地调试，不代表所有 Host App 都必须采用同样 UI。
