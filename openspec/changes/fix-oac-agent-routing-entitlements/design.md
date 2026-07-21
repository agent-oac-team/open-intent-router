## Context

当前 OAC Client 将 edition 作为 `user_tags` 发送给 OAC Go，Go 验证 JWT 后只把 role 转为 `admin/operator` groups 并使用 `OIR-HOST-V1` HMAC 签名。OAC Adapter 验签后把 groups 放入 OIR `UserContext`；与此同时，迁移后的 9 个 Agent 仍把 `运营版/展业版` 保存在 `AccessPolicy.allow_groups`。两侧集合没有交集，因此合法用户在权限过滤阶段失去目标 Agent，Central Route 返回 `unsupported`。

现有 Core 已在构造 LLM 候选前执行 `AgentDefinition.is_available_to(user)`，但通用 `UserContext` 和 `AccessPolicy` 尚无 entitlement 字段；Registry Compat Mapper 仍直接双向复制中文 groups；Evidence 在候选为空时仍可能被调用；Router 只对单一 target 做候选集后置验证，没有覆盖 Plan 的全部 steps。Registry definition 与 registry audit 当前分别提交，无法保证同一数据库事务。

本变更横跨 OAC Go、OAC Host Adapter 与 OIR Core，但仍遵守 Adapter 只能调用 Core 公开应用端口、Core 不认识 OAC/IRS/中文版本标签的既有边界。OAC 已停止运营且处于开发状态，可以在本地和测试环境以受控停写窗口完成权限数据切换，无需建设生产双策略灰度。

## Goals / Non-Goals

**Goals:**

- 用 `PrincipalClaims.entitlements` 与 `AccessPolicy.any_entitlements` 打通 OAC role/edition 到 Agent 候选过滤的正向链路。
- 固化两项首期 ASCII entitlement：`workspace.operations.access` 与 `workspace.sales_enablement.access`，`any_entitlements` 使用 OR 语义。
- 保持 OAC Registry Admin 的 `allowed_user_tags` UI/API 契约，并在 Adapter 内完成可逆投影。
- 使使用现有 Bundle 和执行类型的新 Agent 只需新增或修改 Registry 记录。
- 在 Evidence、触发词、LLM、route override 和 Plan 生成前后都维持同一候选权限边界。
- 以版本化签名、严格 Registry 校验、事务化 revision/audit、幂等迁移和端到端权限矩阵完成切换。

**Non-Goals:**

- 不建设 AuthorizationContext，不把 entitlement/policy snapshot 绑定到 Turn、Plan、Run、Ticket 或 Event。
- 不定义角色或 Policy 变化后的在途执行撤权语义，不修改 execution ticket。
- 不扩展 entitlement action；首期不引入 `invoke/read/write/manage`。
- 不改造 Knowledge Asset 权限，不处理 Coze Workflow 响应后的业务逻辑。
- 不扩展会话生命周期、跨会话 Plan/Task Memory 或历史状态迁移。
- 不让新增 UI 页面或新 Provider 协议变成纯 Registry 配置；它们仍分别需要页面代码或 invoker/plugin。

## Decisions

### 1. Core 只增加通用 entitlement 集合

`UserContext` 增加默认空数组 `entitlements`；Agent `AccessPolicy` 增加默认空数组 `any_entitlements`。Core 对 entitlement 只做去重后的精确字符串匹配，不解析 OAC Bundle 或中文标签。

Policy 评估顺序保持 deny 优先。`any_entitlements` 内部是 OR；它与现有非空 `allow_roles/allow_groups/allow_tenants/required_attributes` 条件之间是 AND。字段为空表示不增加该维度限制，以保持非 OAC Host 向后兼容。OAC Adapter 额外要求 OAC Agent 的 entitlement Policy 非空。

`agent_definitions.access_policy_text` 已以 JSON 保存完整 Policy，因此不新增 entitlement 专用数据库列；schema、file Registry 和序列化测试负责证明往返无损。相比增加一等 entitlement 表，这一方案足以解决当前两个 Bundle 的精确集合判断，避免引入无实际需求的权限图或表达式引擎。

### 2. Bundle 是 Adapter 配置，不是 Agent Registry 实体

OAC Host Adapter 持有版本化 Bundle catalog：

```yaml
policy_version: oac-authz-v1
bundles:
  oac-operations:
    legacy_tag: 运营版
    grants: [workspace.operations.access]
  oac-sales-enablement:
    legacy_tag: 展业版
    grants: [workspace.sales_enablement.access]
```

映射必须一一对应、标签和 Bundle ID 唯一、grants 非空、全部 ASCII 且以 `.access` 结束。启动 readiness 校验映射可逆，禁止首期其他 action。Agent 只引用 entitlement，不引用 Bundle ID；因此使用现有版本新增 Agent 不改变 Bundle catalog。

不把 Bundle 存为 Core 表，是因为 Bundle 属于 OAC 身份与兼容词汇，不是所有 Host 都需要的资源。也不采用每 Agent 一个 entitlement，因为那会使新增 Agent 同时修改 Bundle，破坏配置式注册目标。

### 3. OAC Go 使用当前服务端事实授权 edition

Central Route 进入代理后，OAC Go 解析请求并要求恰好一个已知 `user_tags`。Go 根据 JWT subject 查询当前 users 记录，要求用户存在、`status=active`、`approval_status=approved`，再按固定规则授权活动 Bundle：

- `admin/operator`：`oac-operations`、`oac-sales-enablement`
- `user`：仅 `oac-sales-enablement`

JWT 只证明登录 subject，不再把最多 7 天前的 role 直接作为权限事实。未知、空或多个标签返回 `422`；账号状态或 role 不允许所选 Bundle 返回 `403`；这些请求不发送给 Adapter。

### 4. Agent Route 升级为 OIR-HOST-V2 断言

保留现有 V1 字段和 credential-class 隔离，新增以下受签名字段，并将 canonical 前缀升级为 `OIR-HOST-V2`：

- `X-OIR-Host-Claims-Version: oac-principal-v1`
- `X-OIR-Host-Roles`
- `X-OIR-Host-Active-Bundle-Id`
- `X-OIR-Host-Policy-Version`

V2 canonical 同时绑定 subject、tenant、credential class、HTTP method/path/query、Body SHA-256、audience、timestamp、nonce 和上述 claims。Entitlements 不由 Go 直接声明；Adapter 验证活动 Bundle 与 policy version 后从自身 catalog 展开，构造通用 PrincipalClaims。这样 OAC 是用户/角色/版本选择事实源，Adapter 是权限包翻译边界，Core 只消费通用 claims。

部署兼容窗口内 Verifier 同时识别 V1/V2。Central Agent Route 在 V2 Go 已就绪并完成 Registry 迁移后强制 V2；V1 groups 不得作为 OAC Agent entitlement 的隐式替代。Admin 与 Coze 的非 Agent 路由按 credential class 暂时保留 V1，capability 输出接受版本和 Agent Route 所需版本，但不暴露 claims 或密钥。

未知签名/claims 版本、签名错误、过期或 nonce 重放统一返回 `401 host_authentication_failed`；Body user/user_tags 与受信 subject/Bundle 不一致返回 `403 host_claims_mismatch`。认证或权限失败不进入正常 `unsupported` 分支。

### 5. Registry Compat Mapper 使用纯函数可逆投影

Mapper 只依赖 Bundle catalog：

- `[运营版] -> [workspace.operations.access]`
- `[展业版] -> [workspace.sales_enablement.access]`
- `[运营版, 展业版] -> [workspace.operations.access, workspace.sales_enablement.access]`

输入先去重并按 catalog 稳定顺序规范化。权限为空、未知标签、`bot_id/route_path` 同时为空或同时存在返回 `422 registry_validation_failed`。反向投影仅接受已知 entitlement；未知 entitlement 或迁移后仍混有中文 OAC `allow_groups` 返回 `409 registry_policy_not_legacy_projectable`，不得投影为空列表。

新增 Agent 沿用现有类型推断：仅 `bot_id` 映射为 `provider_platform`，仅 `route_path` 映射为 `ui_handoff`。新增页面或 Provider 协议仍在 Registry 之外建设。

### 6. Registry definition 与 audit 使用通用事务化 mutation port

Adapter 在数据库模式下不得继续先调用 `upsert_definition` 再独立追加 audit。Core 提供通用 Registry mutation application port，接收已完成 OAC 映射的 `AgentDefinition`、expected revision、operator、source 和 operation，在同一 SQLAlchemy session/transaction 中写 definition 与 `RegistryRevisionModel`。Adapter 不把中文标签传给该端口，也不直接访问 Core 私有 repository。

Memory/file 测试替身使用同一命令契约：Memory 以单锁提交 definition 与 audit；file backend 继续拒绝 CRUD。任何验证、revision 冲突或 audit 写入失败都不产生部分 definition。数据库 `409 agent_revision_conflict` 行为保持不变。

不采用补偿式删除审计或异步 outbox，因为 Registry 写入和 audit 位于同一 PostgreSQL 数据库，单事务是更小且可证明的边界。

### 7. entitlement 过滤是所有路由证据的上界

Router 的顺序固定为：

1. 使用受信 PrincipalClaims 加载并过滤 enabled Agent。
2. 候选为空时直接构造 `unsupported`，不调用 Agent Evidence/LLM。
3. 只对已授权候选执行 trigger metadata、Evidence Provider、fixed override、Context 和 LLM 路由。
4. 对 Evidence target、LLM target、current Agent continuation 和 Plan 每个 step 重新验证候选成员关系。
5. 通过后才构造 invocation 或 Delegated Run。

正/负关键词继续作为路由证据，不直接裁剪或恢复权限候选。合法 claims 但没有候选时保持 IRS-compatible `unsupported`；越权 target 或 Plan 返回结构化 RoutingError/安全 fallback，不静默放宽 Policy。

### 8. 现有 9 个 Agent 使用幂等受审计迁移

迁移脚本从当前 OIR database Registry 读取每个 OAC Agent，严格识别 `运营版/展业版` groups，并通过同一事务化 mutation port 写入 `any_entitlements`、清除对应中文 `allow_groups`。脚本验证稳定 ID 和 9 条逐项基线，不修改 trigger、invocation、UI handoff、enabled 或其他 metadata，不重置 revision，也不绕过 audit；迁移本身形成一次正常 revision/audit。

已经具有正确 entitlement 且没有中文 OAC groups 的 Agent 视为 no-op。未知或混合权限停止整批切换并输出具体 Agent，不猜测映射。

## Risks / Trade-offs

- [OAC Go 与 Adapter 的 Bundle/policy version 部署漂移] → V2 同时签名 active Bundle 和 policy version，Adapter 只接受 readiness 公布的组合，并先部署双版本 Verifier。
- [Registry 先迁移导致 V1 用户无候选] → 在停写窗口内先验证 V2 claims，再迁移 9 个 Agent，最后开启 Central Route V2 强制门禁。
- [空 `any_entitlements` 对通用 Core 仍表示无额外限制] → OAC Compat 写入和迁移单独强制非空，避免破坏其他 Host 的既有开放 Agent。
- [反向投影失败使 OAC Admin 列表报错] → 切换前运行全量可投影 readiness 扫描；运行时 fail closed，不把异常 Policy 伪装为公开。
- [Plan 中隐藏无权 Agent] → 后置校验覆盖每个 step，而不仅是顶层 target。
- [事务化 Registry mutation 扩大通用 port] → 端口只接收通用 Agent/audit 数据，不包含 OAC 标签或 Bundle，保持 Core 可复用。
- [本变更不处理权限变化后的在途 Run] → 明确记录为后续 AuthorizationContext/撤权语义工作，不在本轮扩张。

## Migration Plan

1. 在 Core 增加 `UserContext.entitlements`、`AccessPolicy.any_entitlements`、持久化往返与候选过滤测试，保持默认空数组兼容。
2. 在 Adapter 增加 Bundle catalog、纯 Mapper、readiness 和 V1/V2 Verifier，但暂不强制 Central Route V2。
3. 在 OAC Go 增加当前用户状态/角色查询、edition 授权和 V2 signer；运行跨语言 frozen canonical vectors。
4. 在本地验证 `admin/operator/user` 的 V2 claims、Body 一致性与正向 Agent 候选，不迁移 Registry。
5. 启动短暂停写，运行 9 Agent entitlement 迁移与反向投影报告；验证 `production_schedule` 单版和 `strategy_analysis` 双版。
6. 开启 Central Route V2 required，运行完整权限矩阵、Evidence/LLM/Plan 越权负向和 OAC transport E2E。
7. 测试环境执行 100% 权限 replay；阻塞差异为零后保留 V1 仅供 Admin/Coze 兼容路径。

回滚时先关闭 Central Route 新请求，恢复迁移前 9 Agent definition 快照及其中文 groups，再把 OAC Go Route signer 切回 V1；不回滚或修改任何 Turn/Memory 并行工作。由于 Registry 写入有 revision/audit，回滚也必须通过受审计 mutation 完成，不直接覆写数据库。

## Open Questions

本变更没有阻塞实施的产品决策。AuthorizationContext、Ticket 绑定、在途撤权、fallback 可信重投影和知识 entitlement 均明确排除，留待迁移路由恢复后另行提案。
