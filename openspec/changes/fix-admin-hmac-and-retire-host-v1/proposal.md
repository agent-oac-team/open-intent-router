## Why

本地真实传输测试发现，OAC Go 的 Registry Admin 客户端仍只发送旧 `X-Admin-Sync-Token`，而 OIR Host Adapter 已强制 `oac_admin` HMAC，导致 `/api/central-agent-registry` 返回 `502` 并迫使前端使用静态 fallback map，破坏 OIR Registry 唯一事实源和配置式新增 Agent 的目标。与此同时，当前 V1 被用于 Admin/Coze、V2 仅用于 Agent Route；若不在修复后立即收敛，系统会长期按业务类型维护两套 canonical、验证和部署协议。

## What Changes

- 先以最小改动恢复 Registry Admin HMAC：OAC Go 使用真实服务端操作者 `user_id`、`credential_class=oac_admin` 和空 groups 对 Registry GET/POST/PUT/PATCH/DELETE 进行 V1 签名；同次统一 Knowledge Admin 的 Admin identity 构造。
- 保留 `X-Admin-Sync-Token` 一个迁移兼容周期，但明确其不再构成 Registry/Knowledge Admin 授权证据；缺少或错误 HMAC 必须 fail closed。
- 设计并实施单一当前版 Host Signature Envelope，以 credential profile 表达 `oac_user`、`oac_admin`、`coze_workflow` 的不同 claims 约束，而不是按 Route、Registry、Knowledge 分叉 canonical 协议。
- 在双验签窗口内依次迁移 OAC User、Admin 和 Coze signer，提供版本 capability、V1 使用观测和关闭门禁；Registry/Knowledge 写操作始终禁止 fallback 或双写。
- **BREAKING**：完成迁移门禁后，OIR Host 停止接受 `OIR-HOST-V1`，OAC 删除 V1 signer/canonical/测试向量和 Registry 对 `CENTRAL_ADMIN_SYNC_TOKEN` 的认证依赖；浏览器侧 IRS-compatible API 路径、请求和响应 Schema 不变。
- 增加 OAC Go 到真实 OIR Verifier/Registry/Knowledge 的跨进程传输测试，覆盖实际 actor audit、五类 Registry 操作、Admin Knowledge、Coze 只读和全部 fail-closed 场景。

## Capabilities

### New Capabilities

- `oac-host-signature-lifecycle`: 定义 OAC Host User/Admin/Coze 的受信签名 Envelope、credential profiles、Registry/Knowledge Admin 修复、版本迁移、观测门禁和 V1 最终下线要求。

### Modified Capabilities

- 无。

## Impact

- OAC Go：`central_intents.go`、Admin Knowledge 代理、Host signer、配置校验、错误映射与测试。
- OIR Host Adapter：Identity canonical/verifier/models、credential policy、capability/readiness、nonce/replay 验证与契约测试；OIR Core 不引入 OAC 专有字段或反向依赖。
- OAC Client：外部 API 不变；Registry 成功读取后不再依赖静态 fallback map，需增加成功路径和降级告警验收。
- 部署：需要 User/Admin/Coze current key 配置、先验后签的滚动顺序、V1 使用归零证据和最终关闭开关；本地先恢复绿色基线，测试环境优先只运行统一协议。
- 数据：不迁移 Registry definition、Turn、Run、Plan、Memory 或 Knowledge 内容；Registry mutation 继续保留真实 actor、revision 和 audit。
