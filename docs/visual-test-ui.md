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
- 展示 RouteResponse、InvocationResult、Evidence、UI Handoff、Plan 和 Event Response。
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

计划面板支持：

- 刷新 Plan 状态。
- 确认并执行：调用 `/api/v1/plans/{plan_id}/confirm-and-execute`。
- 继续执行：调用 `/api/v1/plans/{plan_id}/execute`。
- 恢复：调用 `/api/v1/plans/{plan_id}/resume`，适合补充输入后继续。
- 取消：调用 `/api/v1/plans/{plan_id}/actions`。

如果后端返回 `next_action=open_ui`，说明需要宿主应用打开页面；如果返回 `next_action=collect_input`，说明需要补充必要参数。

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
