# OAC/Coze Central 与 Knowledge URL 切换矩阵

## 已证实入口

| 消费方 | 公开调用 | 当前目标配置 | 本地切到 Adapter | 测试切到 Adapter | 证据 |
| --- | --- | --- | --- | --- | --- |
| OAC AI Sidebar | 相对路径 `/api/central/*` | 浏览器调用同源 OAC Go，不直接知道 IRS URL | 不改 Client URL；Go `CENTRAL_API_BASE_URL=http://127.0.0.1:<adapter-port>` | 不改 Client URL；测试 Go `CENTRAL_API_BASE_URL=<test-adapter-base>` | `apps/client/src/lib/central-api.ts`、`apps/server/main.go` |
| OAC Go Central Proxy | `/api/central/route`、events、plans | `apps/server/.env` 的 `CENTRAL_API_BASE_URL`，默认 `http://127.0.0.1:8000` | 改为本地 OAC Host Adapter base URL，Go 重启后生效 | 改测试 Go 服务环境变量并重启，生产不存在 | `apps/server/main.go`、`apps/server/.env.example` |
| OAC Go Knowledge Admin Proxy | `/api/admin/knowledge/files*` | 与 Central 共用 Go `CENTRAL_API_BASE_URL` | 与 Go Central 一次切换 | 与 Go Central 一次切换 | `apps/server/central_knowledge.go` |
| OAC Next Knowledge Admin/Exact Read | `/api/admin/knowledge/*` Next Route | `apps/client/.env.local` 的 `CENTRAL_API_BASE_URL`，默认 `http://127.0.0.1:8000` | 改为本地 Adapter base URL，重启/rebuild Client runtime | 改测试 Client server env 并重启 | `apps/client/src/app/api/admin/knowledge/_shared.ts` |
| OAC Next 到 Go 的身份校验 | Next 服务端调用 `/api/auth/me` | `GO_BACKEND_URL`，默认 `http://127.0.0.1:8082` | 保持指向本地 Go，不切到 Adapter | 保持指向测试 Go，不切到 Adapter | `_shared.ts` |

## 路径所有权

- OAC Go `mapCentralProxyPath` 把 `/api/central/knowledge/*` 映射为 `/api/v1/knowledge/*`，其余 Central 子路径映射为 `/api/v1/central/*`。
- OAC Next Knowledge Route 直接使用 `CENTRAL_API_BASE_URL + /api/v1/...`，不经过 Go Central Proxy。
- 因此本地/测试切换必须同时修改 Go 和 Client server 两个进程的 `CENTRAL_API_BASE_URL`；只修改其中一个会形成 Central/Knowledge 分流到不同事实源的错误状态。
- `GO_BACKEND_URL` 不属于 IRS/OIR 切换项，仍用于 OAC 登录态与业务 API。

## Coze 切换口径

已确认 Coze Workflow 只请求 IRS Knowledge API。迁移不盘点或验收 Workflow 内部返回处理，也不要求提供 Workflow ID；Adapter 负责保持原 Knowledge 路径、请求、响应、HTTP 状态和 warning 功能一致。

| 消费方 | 当前目标 | 本地/测试切换 | 验收边界 |
| --- | --- | --- | --- |
| Coze Workflow Knowledge HTTP/API 节点 | IRS Knowledge base URL + 原 `/api/v1/knowledge/*` 路径 | 将 IRS base URL 替换为 Adapter base URL，endpoint path 和请求体保持不变 | Contract fixture 全通过并完成 transport smoke；不验证 Workflow 后续分支或响应处理 |

Coze 具体 URL 存储在节点常量、Workflow 变量或环境变量中的差异只影响切换操作位置，不改变 Adapter 契约或研发实现。测试环境切换任务执行时由 Coze 配置维护者修改实际 base URL。
