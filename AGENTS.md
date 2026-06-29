# AGENTS.md

本文件是给 Codex / coding agent 使用的仓库级工作指令，作用范围为整个 `open-intent-router` 项目。

## 项目背景

`open-intent-router` 是一个面向企业应用和聊天系统的轻量级意图识别与 Agent 编排后端。项目目标是接收自然语言输入，识别用户意图，筛选可用 Agent / Tool / Workflow，返回稳定的结构化路由结果，并在安全可控的情况下执行后端可调用能力。

项目必须保持通用，不绑定 OAC、银行业务、飞书、Coze、Dify、FastGPT 或任何单一宿主系统。业务示例只能作为 sample、fixture 或文档示例，不应进入核心逻辑。

## 常用命令

后端测试：

```bash
.venv/bin/python -m pytest
```

前端测试：

```bash
npm run test
```

前端构建：

```bash
npm run build
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
npm run dev
```

## 架构边界

- `app/api`：FastAPI HTTP 接口，只做协议适配、依赖注入和错误转换。
- `app/schemas`：Pydantic 请求、响应和领域模型，API 契约变更应先从这里明确。
- `app/services/router_service.py`：意图识别、候选 Agent 裁剪、路由决策和 Plan 生成。
- `app/services/invocation_service.py`：单 Agent 调用执行，是 PlanExecutor 复用的底层能力。
- `app/services/plan_service.py`：Plan 保存、确认、取消和事件驱动状态更新。
- `app/services/plan_executor.py`：Plan 步骤执行、依赖推进、结果回填、暂停恢复和 `next_action` 协作。
- `app/services/registry_service.py`：Agent Registry 加载、合并和候选过滤。
- `app/llm`：Mock 与 OpenAI-compatible LLM Client。
- `app/invokers`：Agent 调用器实现，例如 `mock`、`http`、`local_function`、`ui_handoff`。
- `app/plugins/evidence.py`：可选 Evidence Provider，只提供证据、候选收窄或固定问命中。
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
- 新增或修改 API 契约时，同步更新 schema、测试和 `docs/api.md`。
- 新增执行路径时，优先复用 `InvocationService` 和已有 invoker，不创建第二套调用逻辑。
- 新增 Agent 类型时，应同时考虑 Registry schema、invoker 注册、输入构造、输出校验和测试。
- 不把业务专有名称写入核心模块、默认 Prompt 或公共 schema。
- 数据库和本地文件注册表要保持共存：数据库为主，本地文件用于开发和兜底。
- 保持 API 向后兼容；需要破坏性迁移时先通过 OpenSpec 说明。

## OpenSpec 工作流

- 需求或架构调整优先使用 `openspec/changes/<change-name>` 记录 proposal、design、spec 和 tasks。
- 实现 OpenSpec 任务时，先读取对应 change 的全部上下文文件，再逐项实现。
- 完成任务后及时勾选 `tasks.md`。
- 归档前必须运行相关测试和 `openspec validate <change-name> --strict`。

## 测试要求

- 后端逻辑变更至少运行 `.venv/bin/python -m pytest`。
- 前端 UI 或类型变更至少运行 `cd web && npm run test` 和 `cd web && npm run build`。
- Plan、Router、Invocation、Registry、Admin Security 相关变更需要补充或更新对应测试。
- 如果无法运行某项测试，最终回复中必须说明原因和风险。

## 安全与配置

- 不要提交 `.env`、API Key、Token、数据库密码或任何真实凭证。
- `.env.example` 和 `.env.deepseek.example` 只能包含占位值。
- `ADMIN_API_TOKEN` 的策略：本地 `APP_ENV=local` 且未配置 token 时，只允许 loopback Admin 写操作；非 local 环境必须配置 token。
- 不要在日志、测试快照或文档中暴露真实 DeepSeek / OpenAI-compatible API Key。

## 文档要求

- README 和 `docs/` 下关键文档默认使用中文。
- 面向开发者和维护者写文档，避免业务汇报式表述。
- 涉及多意图时，明确说明 `plan` 是主契约，`next_action` 是 Host 协作指令，`show_plan` 是兼容行为。
- 涉及前端测试台时，说明它用于本地调试，不代表所有 Host App 都必须采用同样 UI。
