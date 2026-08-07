# 可视化测试 UI

`web/` 是 `open-intent-router` 的本地开发测试台，用于验证 Agent/Intent 配置、意图识别、路由跳转、基础调用、Plan 和 Agent Event。

它不是生产管理后台，不负责登录、多租户权限或真实密钥管理。

## 启动方式

后端：

```bash
cd open_intent_router
. .venv/bin/activate
uvicorn app.main:app --reload
```

前端：

```bash
cd web
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
cd web
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

若 Plan 已有活动 Delegated Run，而当前 Runtime 没有真实控制通道，取消按钮会收到
`control_unsupported`；Plan 与 state version 保持不变。测试台不得把该响应显示成“取消中”或
“已取消”。

如果后端返回 `next_action=open_ui`，说明需要宿主应用打开页面；如果返回 `next_action=collect_input`，说明需要补充必要参数。

## 聊天窗口与状态面板

测试台采用“中间聊天、右侧状态”的布局：

- 中间聊天窗口只展示用户气泡和中控回答气泡，不再显示“第 N 轮”的调试卡片。
- route-only 和 route-and-invoke 仍然在聊天区上方切换。
- 聊天区提供 5 条固定演示问题按钮，点击后只填入输入框并切换推荐执行模式，不会自动发送。
- 高级上下文仍在聊天区下方折叠配置，用于测试 `source`、`current_agent`、`frontend_context`、`plan_id` 和 `step_id`。
- 右侧 Route 标签展示 `decision.action`、目标 Agent、状态、置信度、原因、消息、候选 Agent 和调用预览。
- 右侧 Plan 标签展示计划状态、执行策略、下一步动作、步骤列表和计划操作按钮。
- 右侧 Context 标签在响应包含 `context.metadata.context_pack` 时展示 purpose/consumer、预算使用、保留/丢弃数量、裁剪统计、来源分组、Provider outcome、Projection hash/version 和每个 Context Item 的治理结果。
- 右侧 Memory 标签展示当前选中轮次的 `memory_context`，包括状态、item 数、scope、relevance/confidence、source、TTL、errors 和原始 JSON。
- 右侧 Knowledge 标签展示当前选中轮次的 `knowledge_context`，包括状态、source IDs、item scores、title、URI、citations、denied source IDs、errors 和原始 JSON。
- 右侧 Evidence 标签展示当前响应中的证据命中。
- 右侧 Debug 标签保留完整 JSON、InvocationResult、UI Handoff 和 Agent Event JSON 提交。

Debug 中的 Agent Event JSON 使用一个仅供测试台消费的 `execution_ticket` 字段。测试台会先移除该
字段，再把值放入 `X-OIR-Execution-Ticket` Header；它不会作为 AgentEvent body 发送。Ticket 必须
来自已经创建该 Delegated Run 的受信调用方，测试台和 Native API 都不会代签。不要把真实 Ticket
保存到截图、日志或长期 fixture；空值提交会按生产契约收到 `401`。

测试台在 local loopback 下使用无签名 `oir-principal-v1` Envelope。该便利只在
`APP_ENV=local` 且请求确实来自 loopback 时有效，不是生产兼容开关。非 local 客户端必须由受信
网关对 Envelope 签名。

M3 起后端返回顶层 `assistant_message` 字段。聊天窗口优先消费 `assistant_message`，缺失时才兼容回退到 `decision.message`；`decision.reason` 仍保留在 Route / Debug 状态区，不再作为普通聊天气泡来源。`next_action.message` 属于 Plan / Host 协作状态，`AgentInvocationResult.message` 属于调用结果摘要，二者都由右侧状态面板展示。

测试台现在以 conversation turn 管理聊天状态。每次提交用户输入都会形成一个 turn，并把该轮自己的 `RouteResponse`、`InvocationResult`、`memory_context`、`knowledge_context` 和 `request_id` 保存在前端状态里。聊天区中控气泡下方会显示本轮 Memory item 数、Knowledge item 数、citation 数、denied source 数和 context 状态；点击任意历史 turn 后，右侧 Route、Plan、Context、Memory、Knowledge、Evidence、Debug 标签都会切到该轮 trace。新响应完成后默认选中新 turn；新对话会清空所有 turn 和选中 trace。

route-only 或后端没有返回 invocation input 时，turn 仍然可选，但 Memory/Knowledge 会显示 unavailable/空态，不会用全局 debug 仓库数据反推“本轮用了什么”。请求失败时，该失败 turn 会显示失败消息和空 trace，避免复用上一轮的 memory/knowledge。

前端 per-turn 选中状态刷新后仍会消失，但后端已在 Route Log 和 `context.metadata.context_trace` 中记录 bounded Trace 摘要。该摘要适合差异比较和回放基础，不包含完整 Prompt、无界原文或完整 structured values，也不等同于完整内容级审计平台。

Memory 标签中的 Formation Decision 操作沿用后端既有策略：

| Pending Decision | 确认 | 拒绝 |
| --- | --- | --- |
| UPDATE | 支持 | 支持 |
| DELETE | 支持，提交前二次确认 | 支持 |
| ADD 或其他操作 | 不支持 | 有安全 `decision_id` 时支持 |
| 缺少 `decision_id` | 不支持 | 不支持，显示关联数据错误和刷新入口 |

按 request/session/turn/run 查询时，后端可以用同租户、同用户、同 Formation Job 的受限支撑事件恢复 `decision_id`，但这些支撑事件不会被加入调用方筛选后的顶层 Events。确认或拒绝成功后，前端重新读取当前选中 Turn 的 Memory Trace；DELETE 继续使用 revision precondition 和破坏性操作确认。Recall 的 `provider_timeout` 与 Formation Decision 相互独立，Recall 失败不会隐藏或禁用确认/拒绝。

运行图中的“沉淀本次记忆”可以展开对话收集、后台队列、候选提取、语义/策略决策、人工处理、长期记忆更新和检索索引七个阶段。人工 pending 使用“待处理”，只有 accepted ADD/UPDATE/DELETE 才会把写入和索引阶段投影为本轮实际更新。

runtime 配置会显示 `context_pipeline_mode`、route Memory 开关和
policy/budget/projection version。切回 `legacy` 只回滚 Router 输入路径，不会关闭
Agent access 或 Memory subject isolation。

## 记忆调试管理

中间区域下方提供只读的记忆调试面板：

- Memory 视图调用 `GET /api/v1/memories/debug`，支持 `user_id`、`tenant_id`、`agent_id`、`scopes`、`limit` 过滤，展示 memory items、memory events 和非敏感 metadata。
- 面板旁展示 runtime Memory backend 摘要，包括 mem0 collection 和 history backend 等非敏感字段。
- Knowledge 调试、资产与索引治理属于外部 `knowledge_sys`，不通过 OIR UI 或 API 承接。
- UI 会对 debug metadata 中疑似 API key、password、token、secret、credential、database URL、connection string、DSN 的字段做脱敏展示。

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
