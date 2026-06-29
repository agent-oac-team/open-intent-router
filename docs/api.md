# API 概览

本文档说明 `open-intent-router` MVP 阶段提供的主要接口边界。接口以 FastAPI 暴露，完整字段定义以代码中的 Pydantic Schema 和运行时 OpenAPI 文档为准。

## 健康检查

- `GET /health`：服务进程健康检查。
- `GET /ready`：服务就绪检查，会校验关键依赖是否可用。

## 路由

### `POST /api/v1/route`

只执行意图识别和路由决策，不调用目标 Agent。

典型返回内容包括：

- 候选 Agent 列表。
- 最终选中的 `target_agent_id`。
- 路由动作，例如 `invoke_agent`、`ui_handoff` 或 `clarify`。
- 置信度、理由、缺失输入、Evidence 命中信息。
- 路由日志 ID，便于后续审计。

### `POST /api/v1/route-and-invoke`

先执行路由，再在目标 Agent 可调用且输入满足要求时执行调用。

适用场景：

- 宿主应用希望后端直接完成“识别意图 + 调用工具”。
- 目标 Agent 是 HTTP API、本地函数或 Mock Agent。
- 调用过程需要统一记录 Agent Run、事件和结果。

如果目标 Agent 只能由前端或宿主系统处理，例如 `ui_handoff`，接口会返回路由交接信息，而不会执行外部调用。

### `POST /api/v1/route-and-execute`

先执行路由，再根据执行策略处理结果：

- 单 Agent 请求：复用现有 invocation 路径。
- 多意图请求且 `execution_policy=auto_execute`：创建 Plan 后由后端 PlanExecutor 推进可执行步骤。
- 多意图请求且 `execution_policy=require_confirmation`：返回 Plan 和 `next_action.type=confirm_plan`，不立即执行。

多意图的主契约是 `plan` 字段。`decision.action=show_plan` 仅保留兼容语义，客户端不应只依赖该 action 判断是否展示计划。

## 显式调用

### `POST /api/v1/invoke`

根据 `agent_id` 显式调用目标 Agent，不再重新做意图识别。

MVP 支持的 Invoker：

- `mock`：返回配置好的 Mock 响应，适合本地开发和测试。
- `http`：调用配置的 HTTP Endpoint。
- `local_function`：调用受信任的本地注册函数。
- `ui_handoff`：返回宿主应用所需的路由交接数据，不执行外部系统调用。

## Agent 查询

公开查询接口：

- `GET /api/v1/agents`
- `GET /api/v1/agents/{agent_id}`
- `POST /api/v1/agents/available`

公开接口会隐藏敏感配置，例如密钥、Header Token、私有调用参数等。宿主应用可通过 `available` 接口按用户角色、用户组、租户和属性过滤可用 Agent。

## Agent 管理

管理接口通常需要传入 Admin Token：

- Header：`X-Admin-Token: <token>`
- 或：`Authorization: Bearer <token>`

本地开发例外：当 `APP_ENV=local` 且未配置 `ADMIN_API_TOKEN` 时，后端只允许来自本机 loopback 的 Admin 写操作。非 local 环境必须配置 `ADMIN_API_TOKEN`；如果 local 环境也配置了 token，则仍然需要传入 token。

接口列表：

- `GET /api/v1/admin/agents`
- `POST /api/v1/admin/agents`
- `PUT /api/v1/admin/agents/{agent_id}`
- `PATCH /api/v1/admin/agents/{agent_id}/enabled`
- `DELETE /api/v1/admin/agents/{agent_id}`
- `POST /api/v1/admin/registry/reload`

说明：

- `database` 模式支持完整 CRUD。
- `file` 模式主要用于只读加载，不适合运行时变更。
- `hybrid` 模式以数据库为主，数据库不可用时才使用本地文件兜底。

## 事件

事件接口用于接收 Agent 执行过程中的状态变化、进度、日志和结果片段。

- `POST /api/v1/events/agent`
- `POST /api/v1/runs/{run_id}/events`

事件 ID 具备幂等语义。重复提交相同事件 ID 不应产生重复副作用。

## Run 查询

- `GET /api/v1/runs/{run_id}`

Run 用于记录一次 Agent 调用的生命周期，包括调用输入、调用状态、结果摘要、错误信息和事件序列。

## Plan

计划接口用于表达需要用户确认或分步执行的 Agent 操作。后端提供轻量同步 PlanExecutor，用于执行可后端调用的步骤；遇到 UI Handoff、缺少输入或外部 Agent Runtime 时，会返回 `next_action` 并暂停。

- `GET /api/v1/plans/{plan_id}`
- `POST /api/v1/plans/{plan_id}/actions`
- `POST /api/v1/plans/{plan_id}/execute`
- `POST /api/v1/plans/{plan_id}/confirm-and-execute`
- `POST /api/v1/plans/{plan_id}/resume`

当前支持的 Plan Action：

- `confirm`：确认继续执行。
- `cancel`：取消计划。

执行策略：

- `return_plan_only`：只返回计划。
- `require_confirmation`：确认后执行。
- `auto_execute`：路由后自动执行可执行步骤。
- `host_managed`：由宿主应用推进。

`next_action` 常见类型：

- `confirm_plan`：等待用户确认。
- `open_ui`：需要宿主应用打开页面。
- `collect_input`：需要补充参数。
- `wait_for_agent_event`：等待外部 Agent 或宿主系统上报事件。
- `none`：无后续动作。

## Session

- `GET /api/v1/sessions/{session_id}/messages`

Session 用于保存用户与路由器之间的消息上下文。MVP 只提供轻量会话消息能力，不实现复杂会话状态机。
