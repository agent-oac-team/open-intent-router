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
