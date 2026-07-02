# 可视化测试 UI

`web/` 是 `open-intent-router` 的本地开发测试台，用于验证 Agent/Intent 配置、意图识别、路由跳转、基础调用、Plan 和 Agent Event。

它不是生产管理后台，不负责登录、多租户权限或真实密钥管理。

## 启动方式

后端：

```bash
cd /Users/lijingtong/project/open_intent_router
. .venv/bin/activate
uvicorn app.main:app --reload
```

前端：

```bash
cd /Users/lijingtong/project/open_intent_router/web
npm install
npm run dev
```

默认前端地址：

```text
http://127.0.0.1:5173
```

Vite 开发服务器会把 `/api`、`/health`、`/ready` 代理到 `http://127.0.0.1:8000`。
如果 8000 端口已被其他本地服务占用，可以把后端启动到其他端口，并通过 `VITE_API_PROXY_TARGET` 指定代理目标：

```bash
uvicorn app.main:app --reload --port 8010
cd /Users/lijingtong/project/open_intent_router/web
VITE_API_PROXY_TARGET=http://127.0.0.1:8010 npm run dev -- --port 5175
```

## 主要功能

- 查看后端健康状态、Registry 状态、LLM Provider、模型、Prompt 文件和 Evidence Provider 状态。
- 查看 Agent 列表。
- 创建、编辑、启用、禁用、删除 Agent Definition。
- 通过 Agent 的 `description`、`capabilities`、`trigger`、`required_inputs` 配置意图识别元数据。
- 在对话框中发送 route-only 或 route-and-invoke 请求。
- 配置 `session_id`、`source`、用户角色、用户组、租户、当前 Agent 和前端上下文。
- 中间区域以正常聊天窗口展示用户消息和中控返回给用户的回答。
- 右侧状态面板通过 Route、Plan、Context、Memory、Evidence、Debug 标签展示 RouteResponse、InvocationResult、Evidence、UI Handoff、Plan 和 Event Response。
- 当响应包含 Plan 时，展示执行策略、下一步动作，并提供确认并执行、继续执行、恢复和取消按钮。

## Agent / Intent 配置

当前项目没有单独的 Intent 表。MVP 中，“配置意图”就是配置 Agent Definition 中会被路由器使用的字段：

- `description`
- `capabilities`
- `trigger.keywords`
- `trigger.positive_examples`
- `trigger.negative_examples`
- `required_inputs`
- `input_schema`
- `access_policy`

当 Registry 是 `file` 模式时，UI 会提示只读。需要完整 CRUD 时，请使用 database 或 hybrid 模式。

当前 `config/agents.example.yaml` 提供四个用于内部演示的 Agent：

- `script_writer`：话术生成。
- `visit_preparation`：访前准备。
- `system_usage_guide`：系统使用指引，使用 `ui_handoff` 演示宿主页面跳转。
- `wealth_knowledge`：理财知识。

这些配置用于演示中控接入形态，不代表生产子牙 Agent 清单。

Admin 写操作策略：

- `APP_ENV=local` 且没有配置 `ADMIN_API_TOKEN` 时，只允许本机 loopback 访问执行写操作，例如 `127.0.0.1`。
- 非 local 环境必须配置 `ADMIN_API_TOKEN`。
- 如果配置了 `ADMIN_API_TOKEN`，即使在 local 环境也需要在 UI 中填写对应 token。

UI 会根据 `/api/v1/runtime/config` 显示当前写入状态：

- `file registry 只读`：Registry 使用 file 模式，不支持 UI 写入。
- `本地 loopback 写入已启用，无需 Admin Token`：database/hybrid 模式、local 环境、未配置 token。
- `写操作需要 Admin Token`：database/hybrid 模式，且后端配置了 token。
- `非 local 环境缺少 ADMIN_API_TOKEN，写操作禁用`：需要先配置 token。

## Mock 与 LLM 模式

实际路由模式由后端 `.env` 决定：

```dotenv
ROUTER_LLM_PROVIDER=mock
```

或：

```dotenv
ROUTER_LLM_PROVIDER=openai_compatible
```

UI 中的 Mock/LLM 选择用于测试提示和状态对照，不会直接修改后端 `.env`。如果修改 `.env`，需要重启后端服务。

## Plan 测试

多意图识别以后，UI 不再只依赖 `decision.action=show_plan` 判断是否展示计划；只要后端响应包含 `plan`，右侧计划面板就会显示。

计划状态位于右侧状态面板的 Plan 标签中，支持：

- 刷新 Plan 状态。
- 确认并执行：调用 `/api/v1/plans/{plan_id}/confirm-and-execute`。
- 继续执行：调用 `/api/v1/plans/{plan_id}/execute`。
- 恢复：调用 `/api/v1/plans/{plan_id}/resume`，适合补充输入后继续。
- 取消：调用 `/api/v1/plans/{plan_id}/actions`。

如果后端返回 `next_action=open_ui`，说明需要宿主应用打开页面；如果返回 `next_action=collect_input`，说明需要补充必要参数。

## 聊天窗口与状态面板

测试台采用“中间聊天、右侧状态”的布局：

- 中间聊天窗口只展示用户气泡和中控回答气泡，不再显示“第 N 轮”的调试卡片。
- route-only 和 route-and-invoke 仍然在聊天区上方切换。
- 聊天区提供 5 条固定演示问题按钮，点击后只填入输入框并切换推荐执行模式，不会自动发送。
- 高级上下文仍在聊天区下方折叠配置，用于测试 `source`、`current_agent`、`frontend_context`、`plan_id` 和 `step_id`。
- 右侧 Route 标签展示 `decision.action`、目标 Agent、状态、置信度、原因、消息、候选 Agent 和调用预览。
- 右侧 Plan 标签展示计划状态、执行策略、下一步动作、步骤列表和计划操作按钮。
- 右侧 Evidence 标签展示当前响应中的证据命中。
- 右侧 Debug 标签保留完整 JSON、InvocationResult、UI Handoff 和 Agent Event JSON 提交。

M3 起后端返回顶层 `assistant_message` 字段。聊天窗口优先消费 `assistant_message`，缺失时才兼容回退到 `decision.message`；`decision.reason` 仍保留在 Route / Debug 状态区，不再作为普通聊天气泡来源。`next_action.message` 属于 Plan / Host 协作状态，`AgentInvocationResult.message` 属于调用结果摘要，二者都由右侧状态面板展示。

Context 和 Memory 标签是为后续 Context Pack 与 Memory 模块预留的状态入口；在当前响应没有相关数据时显示空态，不代表路由错误。

推荐演示顺序：

1. 话术生成：展示单意图路由和 mock 调用。
2. 访前准备：展示另一个单意图能力。
3. 系统指引：展示固定问题 Evidence 强命中和 `ui_handoff`。
4. 多步骤计划：展示 `plan`、`execution_policy` 和 Plan 面板。
5. 理财知识：展示知识问答类 Agent，并说明后续可替换为子牙知识库 Evidence Provider。

## DeepSeek 配置

可以从示例文件复制：

```bash
cp .env.deepseek.example .env
```

然后填写本地真实密钥：

```dotenv
ROUTER_LLM_PROVIDER=openai_compatible
ROUTER_LLM_MODEL=deepseek-chat
ROUTER_LLM_BASE_URL=https://api.deepseek.com
ROUTER_LLM_API_KEY=replace-with-real-key
```

原项目字段映射：

| 原项目字段 | open-intent-router 字段 |
| --- | --- |
| `DEEPSEEK_API_KEY` | `ROUTER_LLM_API_KEY` |
| `DEEPSEEK_MODEL` | `ROUTER_LLM_MODEL` |
| `DEEPSEEK_BASE_URL` | `ROUTER_LLM_BASE_URL` |

`.env` 已被 git ignore，不要提交真实 API Key。
