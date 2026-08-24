# Runtime Catalog 生命周期 Runbook

本 Runbook 约束部署级 Runtime Catalog 的启动、就绪判定和关闭。Catalog 只在应用
lifespan 内创建一次：部署组合显式提供的受信 v2 Adapter 先完成 Descriptor 校验、激活和健康检查，
随后冻结。Core 不隐式激活旧 `mock`、`http`、`local_function` 或 `ui_handoff` Invoker；缺失 v2
Binding 时 Definition 被安全隔离，而非回退。结构性失败（Descriptor、factory、activate）使 Catalog
不可用；健康失败按部署策略分别处理，不在请求路径重新创建 Adapter。

## Container 所有权

Catalog 成功后，Application Runtime 从选中的服务图组合对象读取实际消费者及其 `required database
targets`，只建立这些 Managed Database target（相同 Engine spec 合并为一个物理资源），随后一次性组装完整
`ApplicationContainer` 和后台 Runtime。完整 Core 图的 Memory Service、formation 与 maintenance Runtime
即使在 Memory `off` 时仍是消费者；只有组合图没有任何消费者的 target 才会省略。Container 只公开预组装业务服务、Registry 和
Snapshot 端口；它不公开 Engine、Session Factory 或可变 Settings。HTTP dependency 只能读取当前
已发布的 Container，不能缓存或重新构建服务。自定义组合必须以该 app 已深拷贝的 Settings snapshot
作为工厂输入；不得复用或闭包捕获另一个 app 的 Settings。

OAC Host 采用外层 lifespan：必须先进入并确认 Core Container 已就绪，再建立 Host 自己的 Adapter
Container；退出时先撤销 Host ports，才允许 Core 按逆序停止后台 Runtime、关闭数据库并停止 Catalog。
Core/Catalog 降级时不得发布半成品 Host ports，调用方应得到安全的
`application_runtime_unavailable` 响应。

## 配置

`RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS` 是应用退出或 Catalog 部分激活回滚时，全部
Adapter 按逆序释放资源的总 deadline，单位为秒。

- 默认值为 `5`。
- 取值必须大于 `0` 且不大于 `60`；无效值会被 Settings 配置校验拒绝，进程不会启动。
- 修改该值需要滚动重启应用进程才会生效；不支持请求期热更新。

Adapter 的 `activate`、`dispose` 生命周期钩子和 `health_check` 都必须是 async。同步阻塞释放或
探测不会被注册，从而避免绕过 deadline。

`RUNTIME_CATALOG_HEALTH_TIMEOUT_SECONDS` 是每个已激活 Adapter 运行期健康探测的最大等待
时间，默认 `2` 秒，取值大于 `0` 且不大于 `60`。超时、异常和非 `true` 结果都只投影为固定的
`runtime_adapter_unhealthy`，不把异常文本放入 HTTP 响应。

`APPLICATION_CLEANUP_TIMEOUT_SECONDS` 是 Application Runtime 对每个非 Catalog 清理步骤使用的
最大等待时间，默认 `5` 秒，取值大于 `0` 且不大于 `60`。该 deadline 分别应用于已启动后台
Runtime 的停止和每个唯一受管数据库目标的 dispose；某一步失败、超时或收到取消时，Runtime 仍会
继续尝试后续清理。修改该值需要滚动重启，不支持请求期热更新。

`DATABASE_PROBE_TIMEOUT_SECONDS` 是 `/ready` 对每个唯一 required Managed Database Target 执行
轻量 `SELECT 1` probe 的最大等待时间，默认 `2` 秒，取值大于 `0` 且不大于 `60`。各目标并发且
独立计时；超时、异常或失败只投影为固定 `database_unavailable`，下一次成功 probe 会恢复就绪，
无需重启。它不输出数据库 URL、DSN、凭据或原始异常。修改该值需要滚动重启，不支持请求期热更新。

`RUNTIME_REQUIRED_ADAPTER_KEYS` 是逗号分隔的已注册 Adapter logical key。未列出的 Adapter
属于可选能力：健康失败时只隔离依赖它的 v2 Definition，`/ready` 仍返回 `200 degraded`。
列出的 Adapter 不健康或缺失时，`/ready` 返回 `503`，原因固定为
`runtime_required_adapter_unhealthy` 或 `runtime_required_adapter_missing`。修改上述配置均需
滚动重启；不支持请求期热更新。

## Invocation Call Envelope 配额

每个 Runtime Adapter 调用在受理前由进程级 Invocation Runtime 生成唯一的 Agent Call Envelope。
`AGENT_HTTP_TIMEOUT_SECONDS`（默认 `30`，必须大于 `0`）是 Envelope 的绝对 deadline，也是 Core
等待本地 Runtime Adapter 的最大时间；调用方和 Definition 不能把它延长。deadline 到达时 Core
立即将已受理调用收敛为安全 deadline failure，并取消/观察仍在运行的 Adapter task；Adapter 即使错误地
吞掉取消也不能延长 HTTP/Core 等待时间，且这不表示远端副作用已被取消。以下 `INVOCATION_*`
变量是部署硬上限，所有值在启动时由 Settings 校验，修改后必须滚动重启：

| 变量 | 默认值 | 约束 |
| --- | ---: | --- |
| `INVOCATION_MAX_INPUT_BYTES` | `32768` | 已声明业务输入的 UTF-8 JSON 字节数 |
| `INVOCATION_MAX_CONTEXT_BYTES` | `16384` | Core 受治理 Memory/Knowledge Context 的总字节数 |
| `INVOCATION_MAX_MESSAGE_CHARS` | `4000` | Adapter 成功消息字符数 |
| `INVOCATION_MAX_OUTPUT_BYTES` | `32768` | Adapter structured output 的 UTF-8 JSON 字节数 |
| `INVOCATION_MAX_ARTIFACT_COUNT` | `16` | 输入或输出 Artifact reference 数量 |
| `INVOCATION_MAX_ARTIFACT_METADATA_BYTES` | `2048` | 每个 Artifact metadata 的 UTF-8 JSON 字节数 |

Definition 的 `handling.limits` 只能把这些上限收紧。输入、Context、Artifact 或可确定为空的 required
Context 的预检失败返回安全 `422`，不创建 Run；required Context Provider/受控 Handle 不可用时同样在
受理前以稳定的 `knowledge_unavailable` 失败，不创建 Run。调用受理后发现 Adapter 的 message、output、
Artifact 或 usage 越界/非法，固定投影为 `invocation_invalid_response`。

Artifact 只能是 `artifact://`、`memory://` 或 `https://` 的安全逻辑 locator：前两者只允许 opaque
authority、没有 path；HTTPS 只允许无 port/query/fragment/credentials 的安全 authority/path segments。标题
必须是安全逻辑 locator，metadata 只允许 `size_bytes`、`content_type` 和 `sha256`。它们不是正文、Header
或任意文本的旁路。

默认 Envelope Principal 只有 `subject` 与 `tenant`。如确有必要，
`INVOCATION_ALLOWED_PRINCIPAL_CLAIMS` 和 `INVOCATION_ALLOWED_PRINCIPAL_ATTRIBUTE_KEYS` 可分别填写
逗号分隔的规范 claim 或安全属性键；每项还必须由当前 Definition 请求、由 Adapter descriptor 声明接受。
不要配置 token、authorization、header、credential、secret 或 password 类字段：它们被 Core 拒绝，
不会成为例外通道；`api_key`、`bearer`、`cookie` 和 `signature` 等等价字段同样不能配置。

Invocation Runtime 作为 Application Runtime 的受管后台组件，在 Catalog dispose 前取消并 drain 已越过
调用 deadline 的 Adapter task。Adapter 必须协作响应取消；一直吞掉取消会使受管 cleanup 在既有应用
cleanup deadline 内失败，而不会被静默遗忘或以迟到成功覆盖已完成的 Run。

## Connector Resolver

Deployment 如为某个 Runtime Adapter Binding 配置逻辑 `connector_ref`，必须在应用组合时注入一个窄
Connector Resolver。它不是 Runtime Catalog 或动态插件框架：每次调用只接收已验证 Native Principal、
tenant、冻结 `adapter_key` 和逻辑 reference，并只返回该次 Adapter 执行使用的短生命周期 Connector。
Connector 的 endpoint、credentials、Header 或短时 client 不属于 Definition、Envelope、Run、Result、Trace
或日志；持久 Binding Snapshot 仅可记录逻辑 reference 和安全 revision。

Resolver 返回值必须与当前 tenant、Adapter key 和逻辑 reference 精确一致，并带安全 revision；缺少 Resolver、
缺失/越权/不兼容 Connector 或 Resolver 异常都在 Run 受理前投影为安全
`invocation_binding_unavailable`。Core 无论预检、Adapter 执行、deadline 或取消如何结束都会调用该值的
release；Resolver 的实现也不得把请求凭据或租户状态保存在 Runtime Adapter 或进程单例中。
若 Adapter 吞掉 deadline 取消，Runtime 会强持有并在 Catalog 释放前排空该任务；对应 Connector 的
release 延后至任务真正结束，避免已关闭的私有 capability 被迟到代码继续使用。

部署若注册内置 `http` Runtime Adapter，必须用 `http_runtime_descriptor(...)` 在 Runtime Catalog
factory 中创建它，并让 HTTP Connector 的 operation map 提供 endpoint 与 method。Definition 只能声明
`operation`；Connector Egress Policy 必须显式限定 host、port、method、可发送 Header 和请求/响应大小。
默认 HTTPS、TLS verification、拒绝 redirect，且 Client 仅在 Adapter activate 时创建、Catalog dispose
时关闭。若显式启用 redirect，每一跳均需重新通过同一 Egress Policy；不要在调用路径创建临时 HTTP Client。

## 部署与验收

1. 在部署环境设置 `RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS`、
   `APPLICATION_CLEANUP_TIMEOUT_SECONDS` 与 `DATABASE_PROBE_TIMEOUT_SECONDS`；通常分别保留默认值
   `5`、`5` 与 `2`。
2. 滚动重启实例，并等待应用 lifespan 完成 Catalog 构建。
3. 对每个实例验证 liveness 与 readiness：

   ```bash
   curl -fsS http://127.0.0.1:8000/health
   curl -fsS http://127.0.0.1:8000/ready
   ```

4. `/health` 返回 `200` 只表示进程存活，且不执行 Registry、Adapter 或外部探测；只有
   `/ready` 返回 `200` 才允许实例接收业务流量。可选 Adapter 异常时 `/ready` 返回
   `200 degraded`，包含固定 `reason_code` 和受影响 Definition 计数。Catalog、Primary Registry、
   Core 初始化、required Adapter 或任一 required Managed Database Target 失败时 `/ready` 返回
   `503`，其安全响应只包含 `runtime_reason`，不包含 Adapter 配置、端点、数据库 URL、凭据或原始
   异常。Catalog 激活失败固定为 `runtime_catalog_unavailable`；运行期数据库 probe 失败固定为
   `database_unavailable`。
5. 使用受保护的 `GET /api/v1/admin/runtime/inventory` 查看每个 v2 Definition 的脱敏 Handling、
   binding 状态、隔离原因和 quarantine 计数。该接口不输出 Connector reference、Adapter key、
   endpoint、Header、凭据或原始异常；公开 Catalog 仍只包含 `handling_kind`。

## 失败与回退

- `/ready` 为 `503` 时，从实例日志排查 Descriptor 的重复 key、版本/schema/能力校验、工厂、
  生命周期、Primary Registry、required Adapter 或受管数据库 probe；不要把原始异常、数据库 URL
  或凭据复制到 HTTP 响应。
- 可选 Adapter 健康失败时，先用 Admin inventory 确认受影响的 Definition 与固定隔离原因；恢复
  健康后 Snapshot 会从保留的已校验基线原子恢复，不需要重新读取 Registry Source。
- 若 Host 仍使用旧 Registry wire contract，必须在应用组合时提供受信的 source mapper：它在
  lifespan 将完整来源编译为进程拥有的 v2 Snapshot，并在 Admin Registry reload 时先构建候选、
  再原子替换。Host 的已提交 Registry 写入也必须通过 Core 暴露的 refresh port 重新读取 source，
  并在同一 source-refresh fence 内替换 Snapshot；不得传回请求期已捕获的旧状态。mapper 失败时保留
  last-known-good Snapshot，只记录固定安全原因；不得把旧 wire 字段、端点、凭据或原始异常写入
  inventory、HTTP 或日志。
- 若 deadline 不符合容量或关闭要求，将该变量恢复为已验证值（或删除以使用默认值），再滚动
  重启实例。
- 若某个后续启动步骤失败，lifespan 必须按逆序停止已成功启动的后台 Runtime，随后关闭 Catalog；
  Catalog 自身会在同一 deadline 内逆序释放已激活的 Adapter。
