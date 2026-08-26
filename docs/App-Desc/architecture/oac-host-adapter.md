# OAC Host Adapter

## 边界

`host_apps/oac` 负责组装进程，`host_adapters/oac` 负责 IRS wire-compatible
协议、身份、映射、Shadow 和 Cutover。Adapter 只能调用 `app.application`
暴露的 OIR 应用端口。`app/` 不得反向导入 Host，也不得公开 OAC、IRS、Coze、
飞书、`bot_id` 或 `route_path` 专有字段。IRS runtime fallback 已于
2026-07-31 按 issue #22 退役；兼容协议名称不代表运行时依赖。

首期 Adapter 与 OIR 同仓、同进程；边界允许后续拆成独立服务，不改变 OAC/Coze 的 Legacy HTTP 契约。

## 生命周期所有权

每次 Core lifespan 只创建一个单次 `ApplicationRuntime`：它依次拥有 Runtime Catalog、唯一的
Managed Database targets、完整 `ApplicationContainer` 以及已启动的后台 Runtime。Container 只公开
已组装的业务服务和受限 Registry/Snapshot 端口，不公开 Engine、Session Factory 或 Settings；HTTP
provider 只能从当前请求所属的已发布 Container 取得服务，不能在请求期创建资源或读取进程全局配置。
数据库 target 由该次选中的服务图声明：只有存在 Repository、Service 或 background Runtime 消费者的
target 才进入 lifespan，等价 spec 复用同一个物理 Managed Database。完整 Core 图在 Memory `off` 时仍会
选择 Memory Service 和维护 Runtime，因此不会仅因该模式省略其 target。

OAC Host 在 Core 成功发布完整 Container 后才在外层 lifespan 组装自己的
`OacApplicationContainer`。它只从 Core 的受限服务和端口建立 Legacy Adapter、Host identity、Cutover
guard 与 capability provider；Core 关闭前先撤销这个 Host Container。若 Catalog/Core 未就绪，Host
不得发布部分 Adapter 端口：Legacy 业务入口和 `/capabilities` 返回安全的
`503 application_runtime_unavailable`，而 `/health` 仍仅表示进程存活。

## API Surface

- Central：Route、Navigation Event、Agent Event、Active Plan Snapshot、Plan Confirm、Page Task Completion。
- Registry：GET、POST、PUT、enabled PATCH、DELETE。
- `GET /health`：仅表示 OAC Host 进程存活，不读取 Registry 或探测 Runtime/外部依赖。
- `GET /capabilities`：仅输出版本、模式、依赖健康、Write Fence 和脱敏签名门禁状态；它可以读取
  依赖状态，因此不能作为 liveness probe。
- OIR Native API 挂载于 `/oir/api/v1`，Legacy `/api/v1` 不按 Body 猜测协议。

## Identity

首期强制 `tenant_id=oac`。OAC user、OAC Admin 和 Coze Workflow 使用不同
key/credential class。Adapter 校验 key ID、audience、timestamp、nonce、
正文哈希和签名。Knowledge 调用已外置到 `knowledge_sys`，不经过 OIR Host
Runtime。

所有 Host 请求只使用 `OIR-HOST-V2`。V2 将 subject、roles、active Bundle、claims/policy version、credential class、method/path/query、Body SHA-256、audience、timestamp 和 nonce 一起签名。`oac_user` 使用 `oac-principal-v1/oac-authz-v1` 且必须携带 roles 与 active Bundle；`oac_admin` 使用 `oac-admin-principal-v1/oac-control-v1`；`coze_workflow` 使用 `oac-service-principal-v1/oac-readonly-v1`。Admin/Coze 禁止 roles、groups 和 Bundle。

Adapter 只从受信 active Bundle 展开 `UserContext.entitlements`，不从 Body `user_tags` 或中文 group 生成权限；Body user/edition 与 claims 不一致返回 `403 host_claims_mismatch`。V1、未知 profile、跨类 key、坏签名、过期和 replay 统一返回 `401 host_authentication_failed`。

## Execution Ticket

外部 Agent handoff 前，OIR 创建 Canonical Turn 和 Delegated Run。Adapter 返回可选不透明 `execution_ticket`，绑定 run/turn/owner/agent/plan/step/purpose/expiry/nonce，只保存 hash。Ticket 不进入日志、Trace、Diff、Debug 或报告。旧客户端无 Ticket 时，只有受信关联键唯一命中才允许过渡。

Ticket Store、签名配置和 Service 由 Core 统一组装，OAC Adapter 与 Native Event 共用同一实例。
既有 `OAC_HOST_EXECUTION_TICKET_SECRET` 仍由 Host composition 投影给该 Core Service；OAC
`OIR-HOST-V2`、Legacy body 中可选 `execution_ticket`、无 Ticket 的唯一关联迁移规则和冻结 fixture
均不改变。Native Event 只接受 `X-OIR-Execution-Ticket`，不会复用 OAC Legacy body 投影。

## Page Task Completion

页面任务完成是 OAC Host 持久化的用户声明，不是 Agent Result。OAC 只在已验证本地 Session/Turn 所属，并从受信 Trace 确认同一 Turn 已路由到该 Agent 且对应 UI Handoff 已完成后，以 V2 User Host 身份调用页面完成入口；Trace 暂不可达时 OAC 仅保留可重试的声明，不调用此入口。OIR 只拥有其中 Plan Step 的 Canonical 原子转换。请求必须匹配当前 Plan 的 owner、OIR Session、Step、Agent、版本和 `open_ui` 协作动作；不匹配时返回 Canonical Plan 而不推进。该入口不建立 Delegated Run、Ticket、Agent Event 或 Trace Result，外部执行仍使用原有 Ticket/Event 路径。

## External Execution Binding

OAC Legacy Registry 的 v2 兼容转换只在 Adapter 边界进行：`bot_id` 映射为规范
`external_execution.executor_ref`，`route_path` 映射为 `ui_handoff.route`；这些字段不会进入
OIR Core 的公共运行模型。Host composition 只注入 Adapter 的 source mapper；Core 在 lifespan、
Native Admin reload 和已提交的 OAC Registry 写入后，在一个 source-refresh fence 内重新读取来源、
编译候选并原子替换进程拥有的 v2 Snapshot。映射失败保留 last-known-good Snapshot；不合格的 Legacy
行只以脱敏 locator 和固定 reason 出现在管理员 inventory。OAC 的每个路由请求仍使用独立的 v2
Candidate Set，并叠加当前 Adapter 健康状态；这个请求期 Snapshot 不会替换进程 Snapshot，也不能使
readiness/inventory 落后于 Registry。随后只有 Core 的 Router 决定 Handling。对于已由可信 Snapshot
选中的 External Execution，Core 先通过宿主无关的 External Executor 端口确认该逻辑引用可由当前 Host
承接，再持久化 Delegated Run 并签发既有不透明 Ticket。OAC 只承接通过
`OAC_HOST_EXTERNAL_EXECUTOR_REFS` 显式声明的逻辑引用；未知 `bot_id` 在 Run/Ticket 前被拒绝。
`acceptance_id` 对同一 Route/Turn 重试保持稳定，并由持久化的安全指纹记录跨 worker/process 去重；已有
Run 会复用其已持久化的 canonical binding，而不是再次承接。拒绝、越权或不健康的引用不会创建 Delegated
Run 或 Ticket。Run 和 Trace 只记录受限的 executor 标识及不可逆 binding 指纹，不记录 endpoint、
凭据或私有配置。相同 Canonical Run 的重放会返回同一有效 Ticket；受限唯一约束和 Run 级发行栅栏防止
跨进程双签或失败补偿误终止已签发的 Run。接受记录的请求指纹和 binding 指纹均使用配置密钥的域分隔 HMAC，
避免低熵 principal、entitlement 或 Host binding 值被离线枚举。

如果将来的 Host Route 已可信选中 `handling.kind=invocation`，Adapter 只通过
`InvocationApplicationPort.invoke_from_route` 转交原始受信 Route capability。它不从 Legacy
metadata 推断 Adapter、不能创建 Delegated Run/Ticket，也不能覆盖 Core 的 Handling。当前 OAC Legacy
Registry 只能映射 `external_execution` 或 `ui_handoff`，因此这项端口扩展不改变冻结的 Legacy wire 或
Agent Event callback 契约。

配置任一 External Executor capability 时，Host 必须同时设置
`OAC_HOST_EXECUTION_TICKET_SECRET` 或 Core 的 `EXECUTION_TICKET_SECRET`，以便跨进程重建同一
不透明 Ticket；缺失时启动失败而不是在路由期间留下半完成 Run。

## Governance

- Central 中控调用失败时 fail closed，不代理、重试或回退到 IRS。
- Registry、Event、Plan 和 Memory 写入禁止双写。
- Decision Shadow 使用 `route_decision_shadow`，不创建 Turn/Run/Plan/Message/Memory。
- Cutover watermark 之前的 Agent Event 只写入脱敏隔离审计，不调用 Core。

## Capability

`GET /capabilities` 返回 Adapter/Core/Schema/Policy 版本，Memory/Shadow 模式，
Registry 健康，Write Fence，以及 current=`v2`、accepted=`[v2]`、V1
compatibility disabled、profile/Bundle catalog 健康和按版本/类别/操作/终态
聚合的低基数签名计数。响应不返回 Principal claims、subject、key ID、
entitlement 持有情况、数据库 URL、密钥、Token、签名、正文或 Ticket。
