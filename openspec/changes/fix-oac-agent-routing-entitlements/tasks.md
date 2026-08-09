## 1. Core Principal 与 AccessPolicy 模型

- [x] 1.1 为通用 `UserContext` 增加默认空数组 `entitlements`，实现去空、去重、稳定排序和安全 ASCII 值校验
- [x] 1.2 为 Agent `AccessPolicy` 增加默认空数组 `any_entitlements`，实现字段内 OR、跨非空 Policy 维度 AND 和现有 deny 优先语义
- [x] 1.3 补充 roles/groups/tenant/attributes 旧 Policy 在未配置 entitlement 时的向后兼容测试
- [x] 1.4 验证 database `access_policy_text`、file Registry、公开管理视图和进程重载可无损往返 `any_entitlements`
- [x] 1.5 确认 LLM Candidate/Public 投影不暴露 PrincipalClaims、签名或不必要的完整内部 Policy
- [x] 1.6 更新通用 Agent/User schema 与 API 文档，说明 entitlement 精确匹配和空字段兼容语义

## 2. Registry 事务化 Mutation

- [x] 2.1 定义不含 OAC 字段的 Registry mutation command/application port，覆盖 create、update、enable、disable 和 delete 的 actor/source/expected revision/audit 数据
- [x] 2.2 实现 PostgreSQL definition 与 `RegistryRevisionModel` 共用 session 的单事务 mutation，保留乐观 revision 检查
- [x] 2.3 实现 Memory Registry 单锁 definition/audit 提交，并保持 file backend CRUD 拒绝语义
- [x] 2.4 将 OAC Registry Compat handlers 改为调用通用 mutation port，移除成功写 definition 后再独立 append audit 的部分提交窗口
- [x] 2.5 补充 validation、revision conflict、audit insert failure 和 transaction rollback 后 definition/audit 均无部分写入的测试
- [x] 2.6 补充并发更新只有一个 revision 成功、另一个稳定返回 `409 agent_revision_conflict` 的数据库测试

## 3. OAC Bundle 与 Registry Compat 投影

- [x] 3.1 建立版本化 OAC Bundle catalog，固化 `oac-operations` 与 `oac-sales-enablement` 的标签和两项 `.access` entitlement
- [x] 3.2 实现 Bundle 启动校验：ID/标签唯一、grants 非空、ASCII、只允许 `.access`、双向映射可逆
- [x] 3.3 实现 `allowed_user_tags -> any_entitlements` 纯 Mapper，支持单版/双版去重和稳定顺序且不包含 Agent ID 特例
- [x] 3.4 实现 `any_entitlements -> allowed_user_tags` 纯 Mapper，对未知 entitlement 或混合中文 OAC groups fail closed
- [x] 3.5 对 Registry Compat create/update 增加权限非空、标签已知和 `bot_id/route_path` 严格二选一校验
- [x] 3.6 固化 `422 registry_validation_failed`、`409 registry_policy_not_legacy_projectable` 和现有 revision conflict 的兼容错误响应
- [x] 3.7 更新 Host capability/readiness，输出脱敏 policy/claims 版本和 Bundle 映射健康状态
- [x] 3.8 补充“使用既有 Bundle 新增 provider_platform/ui_handoff Agent 只改 Registry”的正向契约测试

## 4. Adapter V2 受信断言与 PrincipalClaims

- [x] 4.1 扩展 Host identity models，增加 claims version、roles、active bundle 和 policy version，保持 V1 credential class 字段兼容
- [x] 4.2 定义 `OIR-HOST-V2` canonical 和新增 headers，将 claims、Body hash、audience、timestamp、nonce、subject、tenant 与 credential class 全部纳入签名
- [x] 4.3 实现 V1/V2 双版本 Verifier、版本允许列表和统一 `401 host_authentication_failed`，保留 nonce 重放与 key rotation
- [x] 4.4 实现 V2 受信身份到通用 PrincipalClaims 的 Bundle 展开，不从 Body 或中文 groups 生成 entitlements
- [x] 4.5 在 Central Route Mapper 前验证 Body user/user_tags 与受信 subject/active bundle 一致，不一致返回 `403 host_claims_mismatch`
- [x] 4.6 增加 Central Agent Route 的 V2-required 配置门禁；门禁开启后 V1 groups 不得恢复 OAC Agent 访问
- [x] 4.7 保持 Admin/Coze 非 Agent 路由在兼容窗口可按 credential class 使用允许的 V1，并验证 Coze 仍不能执行 control/runtime writes
- [x] 4.8 建立 Python V2 frozen canonical vectors，覆盖字段排序、重复值、未知版本、错误 policy version、过期、篡改和重放

## 5. OAC Go 当前用户授权与 V2 Signer

- [x] 5.1 为 OAC Go Host identity/config 增加 claims version、roles、active bundle、policy version 和 Central Route V2-required 配置
- [x] 5.2 在 Central Route 代理中安全解析请求 Body，要求恰好一个 `运营版/展业版` 标签且保持转发 Body 字节与签名 hash 一致
- [x] 5.3 根据 JWT subject 查询当前 users 记录并读取最新 role、status、approval_status，不直接使用 JWT 中的旧 role 签发权限
- [x] 5.4 实现 `admin/operator` 两版、`user` 仅展业版的服务端 Bundle 授权，以及未知/空/多标签 `422`、账号/角色越权 `403`
- [x] 5.5 实现与 Python 完全一致的 `OIR-HOST-V2` canonical、headers 和 HMAC signer，同时保留 Admin/Coze V1 路径
- [x] 5.6 用共享 frozen vectors 完成 Go/Python 跨语言签名一致性测试
- [x] 5.7 补充角色在 JWT 签发后被修改、账号禁用、审批撤销和 Body user/tag 伪造均 fail closed 的 Go handler 测试
- [x] 5.8 验证现有 Client `user_tags=[edition]` 请求形状无需变化，普通用户展业版和 admin/operator 版本切换保持可用

## 6. Router entitlement 候选边界

- [x] 6.1 调整 Router 顺序，先按 enabled、tenant 和完整 AccessPolicy 过滤 Agent，候选为空时直接返回 `unsupported`
- [x] 6.2 确保候选为空或无权 Agent 命中触发词时不向 Evidence/固定 override/LLM 暴露该 Agent
- [x] 6.3 验证 Evidence route override 与 LLM target 都必须属于 entitlement 过滤后的候选集
- [x] 6.4 为生成、恢复和单步折叠 Plan 增加全部 step Agent 的候选成员校验，越权 Plan 不得展示、确认或执行
- [x] 6.5 验证 `continue_agent` 只有当前 Agent 仍在授权候选集且目标一致时才可返回
- [x] 6.6 补充 `production_schedule` 在运营版正向 open/continue 路由、在展业版负向不可见测试
- [x] 6.7 补充 `strategy_analysis` 在运营版和展业版均可正向路由，以及合法无候选保持 IRS-compatible `unsupported` 测试

## 7. 现有 9 Agent 权限迁移

- [x] 7.1 从现有 reconciliation 基线固化 9 个稳定 Agent ID、中文权限和非权限字段 hash
- [x] 7.2 实现支持 dry-run 的幂等迁移脚本，通过事务化 mutation port 将中文 OAC groups 转为 `any_entitlements`
- [x] 7.3 对已经正确迁移的 Agent 实现 no-op，对未知、空或混合权限停止切换并报告具体 Agent
- [x] 7.4 在临时 PostgreSQL 执行迁移演练，证明 definition、revision 和 audit 一致且重复运行不产生重复变更
- [x] 7.5 逐条验证 triggers、invocation、UI handoff、enabled、稳定 ID 和其他 metadata 未改变，迁移不重置 revision 或绕过 audit
- [x] 7.6 生成正向 Policy 与反向 `allowed_user_tags` 对账报告，至少明确验证 `production_schedule` 和 `strategy_analysis`
- [x] 7.7 增加迁移前快照与受审计回滚命令，不直接覆写数据库或触碰 Turn/Run/Memory 数据

## 8. 端到端权限矩阵与切换门禁

- [x] 8.1 覆盖 admin/operator 在运营版可访问运营版和双版 Agent、在展业版只访问展业版和双版 Agent 的矩阵
- [x] 8.2 覆盖 user 在展业版可访问展业版和双版 Agent、选择运营版于 OAC Go 即返回 `403` 的矩阵
- [x] 8.3 覆盖未知/空/多标签、Body/claims 不一致、未知 Bundle/policy version、V1 Route、坏签名、过期和 replay 的失败矩阵
- [x] 8.4 覆盖 Registry 单版/双版往返、空权限、未知标签、执行目标 XOR、不可投影 Policy、revision conflict 和 audit rollback
- [x] 8.5 覆盖 trigger、Evidence override、LLM target、continue_agent 和 Plan step 均不能恢复或选择无权 Agent
- [x] 8.6 建立 OAC Client -> OAC Go 当前用户授权 -> V2 HMAC -> Adapter PrincipalClaims -> Core Policy -> Registry -> 正向 Agent Route 的 transport E2E
- [x] 8.7 将权限矩阵纳入版本化 replay dataset，要求权限放宽、跨版本 Agent 暴露和合法正向路由失败均为 blocking diff
- [x] 8.8 按迁移顺序演练 Adapter 双版本、OAC Go V2、9 Agent 迁移和 Central Route V2 required，验证每个门禁失败时停止推进

## 9. 文档与最终验证

> 本 change 中的迁移期 V1/V2 双版本描述已由 `fix-admin-hmac-and-retire-host-v1` 的 current-only V2 最终态取代；既有 entitlement 验收证据保持不变。

- [x] 9.1 更新 OIR API、Agent Registry、OAC Host Identity/Adapter、capability 和迁移文档，明确中文标签只存在于 Compat 边界
- [x] 9.2 更新 OAC API/后端文档，说明 edition 服务端授权、当前用户事实查询和 V2 签名错误语义
- [x] 9.3 更新既有 `replace-irs-with-oir-oac-adapter` 需求追踪引用，标明本 change 仅修复 Agent 路由权限且不包含 AuthorizationContext
- [x] 9.4 运行 OIR entitlement/identity/registry/router/migration 聚焦测试和完整 `.venv/bin/python -m pytest`
- [x] 9.5 运行 OIR `.venv/bin/python -m ruff check .` 与 `.venv/bin/python -m ruff format --check .`
- [x] 9.6 运行 OAC `apps/server` 的 `go test ./...` 和现有 OIR Adapter migration verifier
- [x] 9.7 运行 Host Adapter 依赖方向和专有词 fitness tests，证明 Core 未引入 OAC/IRS/中文版本字段
- [x] 9.8 运行 `openspec validate fix-oac-agent-routing-entitlements --strict` 并对账所有规格场景与测试证据
