# Runtime Catalog 生命周期 Runbook

本 Runbook 约束部署级 Runtime Catalog 的启动、就绪判定和关闭。Catalog 只在应用
lifespan 内创建一次：所有受信内置 Adapter 先完成 Descriptor 校验、激活和健康检查，全部
通过后才冻结并接收流量。

## 配置

`RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS` 是应用退出或 Catalog 部分激活回滚时，全部
Adapter 按逆序释放资源的总 deadline，单位为秒。

- 默认值为 `5`。
- 取值必须大于 `0` 且不大于 `60`；无效值会被 Settings 配置校验拒绝，进程不会启动。
- 修改该值需要滚动重启应用进程才会生效；不支持请求期热更新。

Adapter 的 `activate` 和 `dispose` 生命周期钩子必须是 async。同步阻塞释放不会被注册，
从而避免绕过 deadline。

## 部署与验收

1. 在部署环境设置 `RUNTIME_CATALOG_SHUTDOWN_TIMEOUT_SECONDS`；通常保留默认值 `5`。
2. 滚动重启实例，并等待应用 lifespan 完成 Catalog 构建。
3. 对每个实例验证 liveness 与 readiness：

   ```bash
   curl -fsS http://127.0.0.1:8000/health
   curl -fsS http://127.0.0.1:8000/ready
   ```

4. `/health` 返回 `200` 只表示进程存活；只有 `/ready` 返回 `200` 才允许实例接收业务流量。
   Catalog 启动失败时 `/ready` 返回 `503`，其安全响应只包含 `runtime_reason`，不包含 Adapter
   配置、端点、凭据或原始异常。

## 失败与回退

- `/ready` 为 `503` 时，从实例日志排查 Descriptor 的重复 key、版本/schema/能力校验、工厂、
  生命周期或健康检查失败；不要把原始异常复制到 HTTP 响应。
- 若 deadline 不符合容量或关闭要求，将该变量恢复为已验证值（或删除以使用默认值），再滚动
  重启实例。
- 若某个后续启动步骤失败，lifespan 必须按逆序停止已成功启动的后台 Runtime，随后关闭 Catalog；
  Catalog 自身会在同一 deadline 内逆序释放已激活的 Adapter。
