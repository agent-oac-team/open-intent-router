## Why

OAC 当前只把 `admin/operator` 签发为受信 groups，而已迁移的 Agent Registry 仍以 `运营版/展业版` 保存 `allow_groups`，导致受信用户与 Agent 权限集合没有交集，Central Route 在合法请求上返回 `unsupported`。需要用通用 entitlement 统一身份与资源策略，同时保持 IRS-compatible Registry 和 Route 契约不变，并避免把 OAC 版本词汇引入 OIR Core。

## What Changes

- 为通用 Principal/User Context 增加 ASCII entitlement 集合，为 Agent `AccessPolicy` 增加 OR 语义的 `any_entitlements`，并保留现有 roles/groups/tenant/attributes 策略字段供其他 Host 兼容使用。
- 在 OAC Host Adapter 中定义稳定 Bundle 映射：`运营版 -> workspace.operations.access`、`展业版 -> workspace.sales_enablement.access`；Bundle 只在新增版本/权限包时定义，不随 Agent 新增而改变。
- 由 OAC 服务端根据当前用户身份、角色、状态和版本选择建立受信 claims；`admin/operator` 可选择两版，`user` 仅可选择展业版，Body `user_tags` 不作为授权证据。
- 将 OAC Registry `allowed_user_tags` 双向投影为 `access_policy.any_entitlements`，要求权限非空、标签已知、`bot_id/route_path` 首期严格二选一，并保持其他 Agent 字段、revision 和 audit 语义不变。
- 在 Evidence、LLM 和任何触发词路由之前按 tenant 与 entitlement 过滤 Agent；拒绝 Evidence、LLM target 或 Plan step 越过已授权候选集。
- 迁移现有 9 个 OAC Agent 的中文 `allow_groups`，补齐 OAC role/edition 到受信 claims、Core policy、正向/负向 Route 的端到端权限矩阵。
- 本变更不建设 AuthorizationContext，不改变 execution ticket、Plan/Run/Event 跨时态授权、会话生命周期、跨会话恢复或知识权限模型。

## Capabilities

### New Capabilities

- `oac-agent-entitlement-bridge`: OAC 角色和版本选择的服务端授权、Bundle 到 entitlement 的受信身份投影、IRS-compatible `allowed_user_tags` 双向转换及失败语义。

### Modified Capabilities

- `agent-registry`: Agent AccessPolicy 支持通用 `any_entitlements`，OAC Agent Registry 写入实施非空权限、已知标签和执行目标二选一校验，并迁移现有权限数据。
- `intent-routing`: Router 在 Evidence/LLM 之前按 entitlement 过滤候选 Agent，并保证 target Agent 与 Plan steps 不能绕过权限候选集。

## Impact

- OIR Core：`UserContext`/受信 Principal schema、Agent `AccessPolicy`、Registry 持久化、Router 候选过滤和输出校验。
- OAC Host Adapter：Identity claims/verifier/projection、Bundle 配置、Registry Mapper、Central Route Body/claims 一致性检查、capability/policy version 与兼容错误投影。
- OAC Go：从当前服务端用户事实授权 edition，生成版本化受信 claims，停止仅按 role 签发 `admin/operator` groups 作为 Agent 权限。
- 数据：现有 9 个 OAC Agent 从中文 `allow_groups` 幂等迁移到 ASCII `any_entitlements`，不改变其他 Registry 字段或运行态数据。
- 测试与文档：增加完整权限矩阵、Registry 双向投影、未知标签/伪造/不一致 claims、Evidence/LLM/Plan 越权和正向 Agent Route 回归。
