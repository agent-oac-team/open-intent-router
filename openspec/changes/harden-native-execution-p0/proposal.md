## Why

OIR 已具备 Canonical Turn、Run、Result、Plan、Execution Ticket、状态版本和 Outbox 基础，但 Native API 仍允许请求体权限声明、跨所有权读取、无 Ticket 事件写入，并会把合法但未按拓扑排序的 DAG 误报为完成。先闭合这些安全与正确性不变量，才能在不固化错误语义的前提下继续演进通用执行控制面。

## What Changes

- **BREAKING**：Native API 使用宿主无关的 HMAC Principal Envelope 绑定 subject、tenant、roles、groups、entitlements 和授权属性；非本地环境不再接受请求体身份或权限作为授权证据，也不提供不安全兼容开关。
- **BREAKING**：Run、Plan 和 Session 读写统一按 `(tenant_id, principal_id)` 所有权过滤；Native Agent Event 必须使用绑定具体 Delegated Run 的 Execution Ticket。
- 每个受信请求只形成一次 Candidate Set。Direct Invoke 在入口执行首次可用性筛选；Route 后调用复用本次候选结果；延迟 Plan 的确认、执行和恢复请求分别形成新的 Candidate Set。
- PlanExecutor 改用 ready-set 调度，按步骤原始顺序稳定串行选择同时 ready 的 Step；只有全部 Step 完成才将 Plan 标记为完成，无法解释的无 ready 状态按不变量错误处理。
- 补齐 Delegated Run `cancel` 确认命令及真实 application-port conformance，使取消确认原子收敛 Run、Turn、可选 Plan Step 和 Outbox。
- 当前 Runtime 未声明下行取消能力时，活动运行的取消返回 `accepted=false`、`transitioned=false` 和 `reason_code=control_unsupported`，不伪造 `cancelled` 或不可达的 `cancel_pending`。
- 在应用 lifespan 中启动轻量 deadline sweeper，仅对超过 `deadline_at` 的活动 Delegated Run 执行幂等 `timed_out` 收敛；心跳陈旧只保留为观察信号。
- 不提取 `ExecutionRuntime`，不新增通用 `/executions`、`/controls`、轮询、Webhook、消息队列、并行 DAG 或插件化 Runtime。

## Capabilities

### New Capabilities

- `native-principal-boundary`: 定义 Native Principal Envelope、fail-closed 认证、Canonical Ownership 和 Ticket-bound Native Event 写入边界。
- `delegated-run-convergence`: 定义 Delegated Run 取消确认、控制不支持结果、deadline 自动收敛和端口一致性。

### Modified Capabilities

- `intent-routing`: Route 请求必须从受信 Principal 派生 User Context，并在单次请求内形成 Candidate Set。
- `agent-registry`: Available Agents 查询必须使用受信 Principal，不能使用请求体自报权限。
- `agent-invocation`: Direct Invoke 必须先进入当前请求 Candidate Set，Route 后调用复用已有候选结果，Native Event 写入必须具有执行权威。
- `plan-executor`: 改为确定性 ready-set 调度、请求级候选筛选和严格完成判定。
- `plan-orchestration`: 取消活动但不支持控制的运行时返回结构化拒绝，不再直接伪造取消终态。
- `session-context`: Native Session 消息读写必须绑定 Canonical Ownership，不再接受 query/body 自报所有权。

## Impact

- 受影响代码：`app/core/security.py`、Native API routers、Agent Registry/Invocation/Plan/Delegated Run services、memory/database repositories、application ports、应用 lifespan 与配置。
- 受影响契约：Native 身份 Header、`POST /api/v1/agents/available`、Direct Invoke、Run/Session 查询与写入、Native Agent Event、Plan Action 响应。
- OAC `OIR-HOST-V2` 与冻结 Legacy wire contract 保持隔离；Adapter 继续只调用 application ports。
- 需要新增安全负向、跨所有权、无序 DAG、取消确认、控制不支持、deadline 自动收敛和真实 Protocol conformance 回归测试，并同步 Native API、架构说明和配置示例。
- 不引入 OAuth/OIDC/JWT 或新的第三方运行时依赖。
