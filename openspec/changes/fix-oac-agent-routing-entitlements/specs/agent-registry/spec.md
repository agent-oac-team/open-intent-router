## ADDED Requirements

### Requirement: Agent AccessPolicy 支持 entitlement 准入
系统 SHALL 在通用 Agent `AccessPolicy` 中支持 `any_entitlements` 字段，并在该字段非空时要求 PrincipalClaims 至少拥有其中一项 entitlement。`any_entitlements` 内部 SHALL 使用 OR 语义，并与 tenant、role、group 和 attribute 等其他非空 Policy 维度使用 AND 语义。

#### Scenario: Principal 拥有 Agent 接受的 entitlement
- **WHEN** enabled Agent 的 `any_entitlements` 与 PrincipalClaims.entitlements 至少有一项交集，且其他非空 Policy 维度均满足
- **THEN** Agent 通过 AccessPolicy 准入

#### Scenario: Principal 与 Agent entitlement 无交集
- **WHEN** Agent 的 `any_entitlements` 非空且 PrincipalClaims.entitlements 与其没有交集
- **THEN** Agent 不得进入可用或路由候选列表

#### Scenario: 其他 Host 继续使用旧 Policy 字段
- **WHEN** 非 OAC Host 的 Agent 仍使用 roles、groups、tenant 或 attributes Policy 且未配置 `any_entitlements`
- **THEN** Registry 保持现有 Policy 语义，不要求迁移为 OAC entitlement

### Requirement: Registry 持久化和公开视图保留 entitlement Policy
系统 SHALL 在 database、file 和 hybrid Registry 模型中无损持久化 `any_entitlements`，并在受权管理视图中返回该 Policy；面向 LLM 的候选摘要 MUST NOT 暴露不必要的身份或授权内部信息。

#### Scenario: Agent entitlement Policy 往返持久化
- **WHEN** Agent Definition 通过 Registry 创建、更新、重载或进程重启
- **THEN** `any_entitlements` 保持稳定集合语义且 revision/audit 可追踪其变化

#### Scenario: LLM 候选摘要生成
- **WHEN** Router 为已授权 Agent 构造 LLM 候选摘要
- **THEN** 摘要可包含路由所需字段但不包含 PrincipalClaims、签名或完整内部 Policy

### Requirement: OAC Agent 权限迁移保持非权限字段不变
系统 SHALL 将现有 OAC Agent 的中文 `allow_groups` 幂等迁移为对应 `any_entitlements`，并保持 triggers、invocation、UI handoff、enabled、稳定 Agent ID、revision 和 audit 语义不变。

#### Scenario: 运营版 Agent 迁移
- **WHEN** `production_schedule` 当前为 `allow_groups=[运营版]`
- **THEN** 迁移后为 `any_entitlements=[workspace.operations.access]` 且中文 group 不再参与该 Agent 授权

#### Scenario: 双版本 Agent 迁移
- **WHEN** `strategy_analysis` 当前允许 `运营版` 与 `展业版`
- **THEN** 迁移后 Policy 包含两项对应 entitlement，任一版本 Principal 均可通过

#### Scenario: 重复执行迁移
- **WHEN** 迁移脚本对已经完成 entitlement 迁移的 Agent 再次运行
- **THEN** Policy、revision 和审计结果保持幂等，不产生重复 entitlement 或权限放宽
