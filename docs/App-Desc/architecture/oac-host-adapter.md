# OAC Host Adapter

## 边界

`host_apps/oac` 负责组装进程，`host_adapters/oac` 负责 IRS wire-compatible
协议、身份、映射、Shadow 和 Cutover。Adapter 只能调用 `app.application`
暴露的 OIR 应用端口。`app/` 不得反向导入 Host，也不得公开 OAC、IRS、Coze、
飞书、`bot_id` 或 `route_path` 专有字段。IRS runtime fallback 已于
2026-07-31 按 issue #22 退役；兼容协议名称不代表运行时依赖。

首期 Adapter 与 OIR 同仓、同进程；边界允许后续拆成独立服务，不改变 OAC/Coze 的 Legacy HTTP 契约。

## API Surface

- Central：Route、Navigation Event、Agent Event、Active Plan Snapshot、Plan Confirm。
- Registry：GET、POST、PUT、enabled PATCH、DELETE。
- `GET /capabilities`：仅输出版本、模式、依赖健康、Write Fence 和脱敏签名门禁状态。
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
