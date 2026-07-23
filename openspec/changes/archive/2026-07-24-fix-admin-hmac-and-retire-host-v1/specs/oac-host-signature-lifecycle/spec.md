## ADDED Requirements

### Requirement: Registry Admin 在协议收敛前恢复受信 HMAC 链路
系统 SHALL 先恢复 OAC Go 到 OIR Registry Compat API 的受信 Admin HMAC 链路；Registry GET、POST、PUT、enabled PATCH 和 DELETE 在临时兼容阶段 MUST 使用 `credential_class=oac_admin` 的 `OIR-HOST-V1` 签名，且不得仅凭 `X-Admin-Sync-Token` 获得访问权限。

#### Scenario: Registry 五类操作使用临时 V1 Admin HMAC
- **WHEN** OAC Go 在协议收敛前调用任一 Registry Compat 操作
- **THEN** 请求包含可由 OIR Verifier 接受的 V1 Admin HMAC，并绑定最终 method、path、query 和 Body hash

#### Scenario: 旧同步令牌不能单独访问 Registry
- **WHEN** 请求携带有效 `X-Admin-Sync-Token` 但缺少、伪造或过期 Admin HMAC
- **THEN** Adapter 返回统一 `401 host_authentication_failed`，且不得读取或修改 Registry

### Requirement: Admin 身份保留真实操作者且不依赖 groups
系统 SHALL 使用 OAC 服务端认证后的当前操作者 ID 作为 `oac_admin` 签名 subject；Registry mutation MUST 将该 subject 保存为 revision/audit actor。Admin 身份的 roles、groups 和 active Bundle MUST 为空，控制面能力只由受信 key、credential class 和 credential profile 决定。

#### Scenario: Registry 写入记录真实操作者
- **WHEN** 已授权 OAC Admin 新增、修改、启停或删除 Agent
- **THEN** OIR Registry audit 的 actor 等于该 OAC 用户 ID，而不是浏览器 Body、固定服务账号或 JWT 中的角色

#### Scenario: Adapter 未配置 legacy group allowlist
- **WHEN** OAC Host 的 legacy allowed groups 为空且合法 Admin 请求使用空 groups 签名
- **THEN** Verifier 仍接受该 Admin 身份并按 `oac_admin` policy 授权

### Requirement: Registry 与 Knowledge Admin 共享一致的 Admin identity 构造
系统 SHALL 由 OAC Go 使用同一 Admin identity 构造规则签发 Registry 和 Knowledge Admin 请求，二者 MUST 使用 Admin current key、真实 actor、`principal_type=user`、`credential_class=oac_admin` 和空 roles/groups/Bundle。

#### Scenario: Knowledge Admin 不再受 legacy groups 配置影响
- **WHEN** operator 或 admin 通过 OAC Go 调用允许的 Knowledge Admin 操作
- **THEN** Go 使用共享 Admin identity 签名，Adapter 在不依赖 legacy groups allowlist 的情况下完成认证和授权

#### Scenario: 非 Admin credential 尝试控制面写入
- **WHEN** `oac_user` 或 `coze_workflow` credential 调用 Registry 或 Knowledge Admin 写操作
- **THEN** Adapter 返回 `403 host_operation_forbidden`，且禁止 fallback、双写或部分提交

### Requirement: V2 成为唯一当前版 Host Signature Envelope
系统 SHALL 将现有 `OIR-HOST-V2` 扩展为 User、Admin 和 Coze 共用的唯一当前版 Envelope，保持既有 Route V2 canonical 字段顺序和冻结向量不变，并通过 credential profile 约束不同身份的 claims；系统 MUST NOT 按 Route、Registry 或 Knowledge 维护不同 canonical 算法。

#### Scenario: 既有 Agent Route V2 向后兼容
- **WHEN** OAC User 使用现有 `oac-principal-v1`、`oac-authz-v1`、非空 roles 和 active Bundle 发送 Route 请求
- **THEN** 扩展后的 V2 Verifier 继续接受原冻结向量且 Route 权限语义不变

#### Scenario: Admin 使用 V2 credential profile
- **WHEN** Registry 或 Knowledge Admin signer 完成统一协议迁移
- **THEN** 请求使用 `oac-admin-principal-v1`、`oac-control-v1`、空 roles/groups/Bundle 和 `credential_class=oac_admin`

#### Scenario: Coze 使用 V2 credential profile
- **WHEN** Coze Workflow 通过 OAC Go 调用 Knowledge 只读接口
- **THEN** 请求使用 `oac-service-principal-v1`、`oac-readonly-v1`、空 roles/groups/Bundle、`principal_type=service` 和 `credential_class=coze_workflow`

### Requirement: Credential profile 严格校验字段与密钥类别
系统 SHALL 为每个 credential class 定义唯一 principal type、claims version、policy version、必填字段、禁止字段和允许操作；key ID MUST 绑定允许的 credential class，未知或跨类组合必须统一认证失败。

#### Scenario: User profile 缺少 Bundle 或 roles
- **WHEN** `oac_user` V2 请求缺少 active Bundle、roles、claims version 或 policy version
- **THEN** Verifier 返回 `401 host_authentication_failed`，不得降级为 groups 或 V1 语义

#### Scenario: Admin 或 Coze 注入 Route claims
- **WHEN** `oac_admin` 或 `coze_workflow` V2 请求携带 roles、groups 或 active Bundle
- **THEN** Verifier 返回 `401 host_authentication_failed`，不得把多余 claims 解释为更高权限

#### Scenario: 凭证 key 跨 credential class 使用
- **WHEN** Coze key 声明 `oac_admin` 或 Admin key 声明 `oac_user`
- **THEN** Verifier 返回统一认证失败且不泄漏 key 是否存在

### Requirement: OAC Go 在签名前使用当前授权事实
系统 SHALL 在签发控制面身份前读取当前 OAC 用户事实；Registry/Knowledge Admin 写入必须校验 subject、当前 role、status 和 approval status，浏览器 JWT 中的旧 role、Body user 或自报 Header MUST NOT 成为授权证据。

#### Scenario: 已禁用或审批撤销的 Admin
- **WHEN** JWT 仍有效但当前用户已禁用、删除、未审批或审批撤销
- **THEN** OAC Go 在生成 HMAC 前拒绝请求，且不访问 OIR Adapter

#### Scenario: JWT role 与数据库 role 不一致
- **WHEN** JWT 记录 admin 但数据库当前 role 已降级
- **THEN** OAC Go 按数据库当前事实执行 Registry/Knowledge 权限判断并拒绝不再允许的写入

### Requirement: V2 继续提供完整请求绑定与重放防护
系统 SHALL 在统一 V2 中签入 key ID、audience、timestamp、nonce、method、规范化 path/query、Body SHA-256、principal、tenant、credential class 和 profile versions；Adapter MUST 校验时钟窗口、nonce、正文 hash、签名和环境 audience，并对失败返回无差别认证错误。

#### Scenario: 请求正文或目标被篡改
- **WHEN** 代理之后的 Body、method、path 或 query 与签名时不一致
- **THEN** Adapter 返回 `401 host_authentication_failed` 且不执行操作

#### Scenario: 重放或跨环境使用签名
- **WHEN** 相同 key/nonce 被再次使用，或 local 签名被发送到 test audience
- **THEN** Adapter 拒绝请求且不暴露具体失败字段

### Requirement: 签名版本迁移具有可观测门禁
系统 SHALL 在迁移窗口公开脱敏的 current version、accepted versions、V1 compatibility 状态和按 credential class/operation 聚合的版本使用计数；系统 MUST NOT 暴露 key ID、subject、claims、Token、签名或正文。

#### Scenario: Adapter 双验签迁移窗口
- **WHEN** Adapter 已支持统一 V2 但部分 Go signer 尚未切换
- **THEN** capability 显示 current=`v2`、accepted 包含 V1/V2、V1 compatibility enabled，并记录脱敏版本计数

#### Scenario: V1 下线门禁检查
- **WHEN** 准备关闭 V1
- **THEN** 本地及测试 replay 必须证明 User/Admin/Coze 的 V1 计数为零、所有 current profile 正向通过且负向 fail closed

### Requirement: V1 在迁移完成后被完全禁用和移除
系统 SHALL 将 `OIR-HOST-V1` 视为迁移兼容协议；门禁满足后 Adapter MUST 停止接受 V1，OAC MUST 停止签发 V1，并删除 V1 canonical、分支、专用测试向量及 Registry 对旧同步令牌的认证依赖。

#### Scenario: 下线后收到 V1 请求
- **WHEN** 任意 User、Admin 或 Coze 调用方在 V1 下线后发送 `v1=` 签名
- **THEN** Adapter 返回统一 `401 host_authentication_failed`，不得自动升级、回退或按旧 Token 放行

#### Scenario: 最终 capability 只报告当前版本
- **WHEN** V1 清理完成且 Host 启动
- **THEN** capability 的 accepted/current 均只包含 V2，代码和运行配置不再提供 V1 启用开关

### Requirement: 外部兼容 API 与单写治理保持不变
系统 SHALL 在签名协议迁移期间保持 OAC Client 和 Coze 所见的 IRS-compatible 路径、请求与响应 Schema 不变；Registry 和 Knowledge Admin 写操作始终只写 OIR，禁止 IRS fallback、盲目重试或双写。

#### Scenario: OAC Client 无需感知签名升级
- **WHEN** OAC Client 读取 Registry、发送 Route 或执行 Admin 操作
- **THEN** 浏览器请求形状和业务响应保持兼容，Host 签名只存在于 Go 到 Adapter 的内部传输

#### Scenario: 控制面请求提交状态不明
- **WHEN** Registry 或 Knowledge Admin 请求超时且无法证明 OIR 未提交
- **THEN** OAC Go fail closed 并返回明确错误，不得转发 IRS 或自动重放写入

### Requirement: 真实传输测试覆盖控制面和协议退役
系统 SHALL 建立 OAC Go 到真实 OIR Host Verifier、Registry 和 Knowledge handler 的传输测试，不能只使用接受任意 Header 的 Stub 或直接注入 `TrustedHostIdentity`。

#### Scenario: Registry 配置式 Agent 完整验收
- **WHEN** 测试使用现有 Bundle 和执行类型创建临时 Agent
- **THEN** create、list、update、enable/disable、route 可见性、delete 和 actor audit 全部通过，且 OAC UI Registry 读取不触发 fallback map

#### Scenario: 三类 credential 协议矩阵
- **WHEN** 测试回放 User Route、Admin Registry/Knowledge 和 Coze Knowledge 请求
- **THEN** 每类 current profile 的正向请求成功，跨类、篡改、过期、重放、旧 V1 和越权请求均按规范失败
