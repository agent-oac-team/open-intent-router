## ADDED Requirements

### Requirement: OAC 版本 Bundle 使用稳定通用 entitlement
系统 SHALL 在 OAC Host Adapter 配置中将 `oac-operations` 定义为 `legacy_tag=运营版` 且授予 `workspace.operations.access`，将 `oac-sales-enablement` 定义为 `legacy_tag=展业版` 且授予 `workspace.sales_enablement.access`。首期 entitlement action MUST 只有 `access`，Bundle 映射 MUST NOT 包含 Agent ID。

#### Scenario: 使用现有版本新增 Agent
- **WHEN** Admin 为新 Agent 选择已有的 `运营版` 或 `展业版`
- **THEN** Adapter 使用既有 Bundle 映射生成 Agent Policy，系统无需修改 Bundle、Adapter 映射或 OIR Core

#### Scenario: Bundle 配置包含未知 action
- **WHEN** OAC Bundle 尝试授予以 `invoke`、`read`、`write` 或 `manage` 结尾的 entitlement
- **THEN** Host Runtime readiness 失败且不得接受 Agent Route

### Requirement: OAC 服务端授权活动版本
OAC 服务端 SHALL 使用当前用户身份、角色、账号状态和审批状态授权每次 Agent Route 的活动 Bundle；`admin/operator` SHALL 可选择两个已定义 Bundle，`user` SHALL 只能选择 `oac-sales-enablement`，Body `user_tags` MUST NOT 单独构成授权证据。

#### Scenario: Operator 选择运营版
- **WHEN** 当前有效且已审批的 `operator` 请求 `运营版`
- **THEN** OAC 服务端签发活动 Bundle 为 `oac-operations` 的受信断言

#### Scenario: 普通用户伪造运营版
- **WHEN** 当前角色为 `user` 的调用方提交 `user_tags=[运营版]`
- **THEN** OAC 服务端返回 `403` 且不向 Adapter 签发该 Bundle

#### Scenario: 用户状态不允许访问
- **WHEN** 用户不存在、被禁用、待审批或审批被拒绝
- **THEN** OAC 服务端拒绝 Route 且不依赖客户端 JWT 中的旧角色继续签发权限

### Requirement: Agent Route 使用版本化受信断言
OAC Go 与 Adapter SHALL 使用包含 subject、roles、active bundle、policy version、Body hash、audience、timestamp 和 nonce 的版本化 HMAC 断言建立 PrincipalClaims。Adapter SHALL 将活动 Bundle 展开为通用 `entitlements`，并 SHALL NOT 从未受信 Body 生成 entitlement。

#### Scenario: 新版受信断言建立 PrincipalClaims
- **WHEN** Adapter 收到签名正确、版本受支持且活动 Bundle 已知的 Agent Route
- **THEN** Adapter 生成包含对应 ASCII entitlement 的 PrincipalClaims 并交给 OIR Core

#### Scenario: Body 与受信 Bundle 不一致
- **WHEN** Body `user_tags` 的规范化结果与签名活动 Bundle 的 legacy 投影不一致
- **THEN** Adapter 返回 `403` 且不得调用 Router、Evidence 或 LLM

#### Scenario: Agent Route 使用旧断言
- **WHEN** OAC Central Route 仅携带旧版 groups 断言而没有活动 Bundle
- **THEN** Adapter fail closed，返回统一认证错误且不得回退为中文 group 授权

#### Scenario: 非 Agent 路由路径处于兼容窗口
- **WHEN** Admin 或 Coze 的既有非 Agent 路由请求使用仍在允许列表内的旧断言版本
- **THEN** Adapter 可按该 credential class 的既有操作策略处理，且 capability 必须公开受支持版本

### Requirement: allowed_user_tags 与 entitlement Policy 可逆投影
OAC Registry Compat API SHALL 使用不包含 Agent ID 的固定映射，在 `allowed_user_tags` 与 `access_policy.any_entitlements` 之间双向转换。中文标签 MUST NOT 写入迁移后的 OAC Agent Core Policy。

#### Scenario: 单版本 Agent 写入
- **WHEN** Admin 写入 `allowed_user_tags=[运营版]`
- **THEN** Adapter 原子生成 `any_entitlements=[workspace.operations.access]`

#### Scenario: 双版本 Agent 写入
- **WHEN** Admin 写入 `allowed_user_tags=[运营版, 展业版]`
- **THEN** Adapter 原子生成包含两项 entitlement 的 OR Policy

#### Scenario: Registry 读取反向投影
- **WHEN** OAC Compat API 读取只包含已知 OAC entitlement 的 Agent Policy
- **THEN** Adapter 按稳定顺序返回等价 `allowed_user_tags`

#### Scenario: Policy 无法兼容投影
- **WHEN** OAC Compat API 读取包含未知 entitlement 或混合中文 `allow_groups` 的迁移后 OAC Agent
- **THEN** Adapter 返回 `409 registry_policy_not_legacy_projectable`，不得用空标签或放宽权限掩盖不一致

### Requirement: Registry Compat 写入执行严格校验
OAC Registry Compat 写入 SHALL 要求权限非空、标签全部已知，并要求 `bot_id` 与 `route_path` 首期严格二选一；验证、映射、Agent revision 写入和审计 MUST 作为一个逻辑提交处理。

#### Scenario: 新 Agent 使用既有执行类型
- **WHEN** Admin 提交非空已知标签并且只配置 `bot_id` 或 `route_path` 之一
- **THEN** 系统只通过 Registry 配置完成 Agent 新增，并在启用后使其进入相应 entitlement 的候选集

#### Scenario: Registry 输入非法
- **WHEN** 权限为空、标签未知、两个执行目标都为空或两个执行目标同时存在
- **THEN** Adapter 返回 `422 registry_validation_failed` 且 Registry 与审计均无部分写入

#### Scenario: Registry revision 冲突
- **WHEN** Admin 基于过期 revision 更新 Agent
- **THEN** Adapter 返回 `409 agent_revision_conflict` 且不覆盖当前 Policy

### Requirement: 路由权限失败保持兼容且 fail closed
Adapter SHALL 区分身份/claims 失败、Registry 配置失败和合法无候选路由，不得将权限错误降级为未受信 groups、Body 标签或触发词授权。

#### Scenario: 合法用户在活动版本中没有候选 Agent
- **WHEN** claims 有效但没有 enabled Agent 的 Policy 接受其 entitlement
- **THEN** Central Route 返回 IRS-compatible `unsupported`，且不向 LLM 暴露无权 Agent

#### Scenario: 未知标签或 claims 不一致
- **WHEN** 请求包含未知标签或受信 Bundle 与 Body 选择不一致
- **THEN** 系统返回 `422` 或 `403` 对应错误，且不得将该请求视为正常 `unsupported`
