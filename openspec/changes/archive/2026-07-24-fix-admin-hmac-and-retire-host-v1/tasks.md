## 1. 失败基线与协议清单

- [x] 1.1 固化 OAC `/api/central-agent-registry` 经 token-only 请求得到 Go 502、OIR 401 和 Client fallback map 的本地失败证据
- [x] 1.2 建立 Registry GET/POST/PUT/enabled PATCH/DELETE、Knowledge Admin、Coze Knowledge 和 Agent Route 的调用方/路径/credential/signature version 清单
- [x] 1.3 标记 `X-Admin-Sync-Token` 在浏览器/Next -> Go ingress 与 Go -> OIR downstream 两个信任边界中的不同用途
- [x] 1.4 为现有 Route V2 Go/Python frozen vector 增加不可变回归断言，防止统一协议时改变 canonical 字节
- [x] 1.5 增加真实 transport 失败测试骨架，禁止通过无条件接受 Header 的 Stub 或覆写 `TrustedHostIdentity` 绕过 Verifier

## 2. OAC 当前操作者与控制面授权

- [x] 2.1 定义 request-scoped Registry/Knowledge Admin actor，至少包含受信 subject ID 和请求 context，不接受 Body 自报 actor
- [x] 2.2 实现签名前当前用户事实查询，读取存在性、role、status 和 approval status
- [x] 2.3 为 enabled Registry read、管理列表、Knowledge Admin 和 Registry mutation 固化现有角色矩阵
- [x] 2.4 让 Registry/Knowledge handlers 将服务端认证后的 actor 和 context 传入所有 downstream helper
- [x] 2.5 对已删除、禁用、未审批、审批撤销、未知 role 和数据库不可用分别实现 fail-closed 响应
- [x] 2.6 补充 JWT role 与数据库当前 role 不一致、Body/Header actor 伪造和普通用户写入的 OAC Go 测试

## 3. Registry Admin V1 有界修复

- [x] 3.1 在 OAC Go 建立 Registry/Knowledge 共用的 Admin identity builder，使用 current Admin key、真实 actor、`principal_type=user`、`credential_class=oac_admin` 和空 roles/groups/Bundle
- [x] 3.2 将 Registry GET list 请求在最终 URL/query 固定后交给现有 V1 Host signer
- [x] 3.3 将 Registry POST/PUT/enabled PATCH 在最终 JSON Body 字节固定后交给现有 V1 Host signer，并验证 signer 读取后恢复 Body
- [x] 3.4 将 Registry DELETE 和包含转义 Agent ID 的路径按 `EscapedPath` 正确纳入 V1 canonical
- [x] 3.5 让 Registry downstream HTTP 请求继承原 handler context，且不转发浏览器提供的 `X-OIR-Host-*` 或 Authorization Header
- [x] 3.6 保留 `X-Admin-Sync-Token` 为可选兼容 Header，但移除 Registry 生成 HMAC 对该 Token 的必填依赖
- [x] 3.7 将 Knowledge Admin 切到共享 Admin identity builder，移除 role -> V1 groups 投影
- [x] 3.8 区分 signer 配置失败、OIR 401/403、Registry 404/409/422 和提交未知超时的 Go 错误投影
- [x] 3.9 补充五类 Registry V1 签名、空 groups、真实 actor、query/body/path 绑定和 Knowledge Admin 回归单测
- [x] 3.10 增加 token-only、错误 Admin key、跨环境 audience、篡改 Body、过期和 replay 的 V1 修复阶段负向测试
- [ ] 3.11 在本地真实 OIR Verifier 上验证 Registry 五类操作和 Knowledge Admin 全部恢复，不再出现 401/502（临时 V1 阶段未单独留存真实 transport；最终 V2 current-only 真实 transport 已完成）

## 4. 通用 V2 Credential Profile

- [x] 4.1 定义不进入 OIR Core 的 Host credential profile catalog，包含 principal type、claims/policy version、字段约束和允许操作
- [x] 4.2 保持 `oac_user` 的 `oac-principal-v1`、`oac-authz-v1`、roles 和 active Bundle 规则完全不变
- [x] 4.3 增加 `oac_admin` 的 `oac-admin-principal-v1` / `oac-control-v1` profile，禁止 roles/groups/Bundle
- [x] 4.4 增加 `coze_workflow` 的 `oac-service-principal-v1` / `oac-readonly-v1` profile，要求 service principal 并禁止 roles/groups/Bundle
- [x] 4.5 将 V2 Verifier 从硬编码 `oac_user` 改为 profile catalog 精确校验，且保持 key ID 到 credential class 的绑定
- [x] 4.6 对未知 profile/version、跨类 key、额外 claims、缺少必填 claims 和 principal type 不匹配统一返回认证失败
- [x] 4.7 保持 method/path/query/body hash、tenant、audience、timestamp、nonce 和签名校验顺序及无差别错误语义
- [x] 4.8 增加 Admin 与 Coze V2 Go/Python frozen vectors，并证明现有 User Route V2 vector 逐字节不变
- [x] 4.9 覆盖三类 profile 的正向解析、字段规范化、跨类伪造、篡改、过期、replay 和 operation policy 测试
- [x] 4.10 运行 Adapter/Core 边界扫描，确认 profile/canonical/OAC 词汇没有进入通用 Core schema、service 或 prompt

## 5. Capability、配置与版本观测

- [x] 5.1 扩展脱敏 capability，报告 current signature version、accepted versions、V1 compatibility 状态和 credential profile catalog 健康
- [x] 5.2 增加按 signature version、credential class、operation 聚合的低基数使用计数，不记录 subject、resource、key ID、签名或正文
- [x] 5.3 为 V1 验签成功、V2 验签成功、认证失败和授权失败建立互斥计数语义
- [x] 5.4 增加 OAC Host 启动期 profile/key 配置校验，User/Admin/Coze current key 缺失或 class 冲突时 fail fast
- [x] 5.5 增加迁移期 `v1_compatibility_enabled` 配置，默认值、环境限制和最终删除条件必须明确
- [x] 5.6 验证 capability/readiness 不泄漏 secret、key ID、subject、完整 claims、Token、Ticket、数据库或 fallback 地址
- [x] 5.7 补充 capability current/accepted/compatibility/counter 的测试和迁移门禁解析器

## 6. OAC Go 全量切换 V2

- [x] 6.1 扩展 Go Host identity/signing 配置以支持 Admin 和 Coze 的非空 claims/policy profile version
- [x] 6.2 将 Registry Admin signer 从临时 V1 切换到 V2 Admin profile，保持真实 actor、空 roles/groups/Bundle 和所有请求绑定字段
- [x] 6.3 将 Knowledge Admin signer 切换到相同 V2 Admin profile，保持现有浏览器 API 和业务响应
- [x] 6.4 将 Coze Knowledge signer 切换到 V2 service profile，保持 ingress token、只读路径和响应后处理边界不变
- [x] 6.5 保持 Agent Route 现有 V2 User profile、edition 授权和 Body 字节不变，不引入 V1 groups fallback
- [x] 6.6 更新 OAC Go 启动校验，要求三类 current profile 配置完整并拒绝 legacy history import
- [x] 6.7 对 User/Admin/Coze signer 增加共享 Envelope 和 profile-specific claims 测试，禁止复制三套 canonical 实现
- [x] 6.8 验证浏览器 Authorization、Coze ingress token、旧同步 Token 和任何入站 Host Header均不会作为 downstream 最终授权证据

## 7. 真实传输与业务验收

- [x] 7.1 建立隔离 OAC PostgreSQL、OIR Registry/Knowledge 数据库和固定 fixture keys 的可重复本地 transport 环境
- [x] 7.2 通过真实 OAC Go -> OIR V2 Verifier 完成 Registry create/list/update/enable/disable/delete 全生命周期
- [x] 7.3 验证 Registry definition 与 revision/audit 原子提交、expected revision conflict、真实 actor ID 和非权限字段不变
- [x] 7.4 新增一个使用现有 Bundle 与 provider 类型的临时 Agent，验证无需 Adapter/Core 代码即可进入对应 entitlement 候选集
- [x] 7.5 验证 OAC `/api/central-agent-registry` 返回 200、Client 使用 OIR Registry 数据且不记录 fallback map warning
- [x] 7.6 通过真实 V2 Admin profile完成 Knowledge Admin list/read/write/delete 或可回滚 fixture 生命周期
- [x] 7.7 通过真实 V2 Coze profile完成 6 个资产发现和 `01` 资产 44 chunks Exact Read，并验证写入返回 403
- [x] 7.8 回放 User Route 的 admin/operator/user × 运营版/展业版权限矩阵，确认统一协议未改变 entitlement 候选结果
- [x] 7.9 回放错误 key/audience/profile、跨类 credential、Body/target 篡改、过期、replay、账号撤权和旧 role 的 fail-closed 矩阵
- [x] 7.10 模拟 Registry/Knowledge 写超时与提交未知，证明不存在 IRS fallback、双写或盲目自动重试

## 8. V1 下线与代码清理

- [x] 8.1 在本地 replay 中证明 User/Admin/Coze 的 V1 成功计数为零且所有 current profile 门禁通过
- [x] 8.2 在测试环境执行 100% transport replay，记录 V1 零使用、current profile 正向和负向证据
- [x] 8.3 关闭 Adapter V1 acceptance，确认所有 `v1=` 请求统一 401 且不会按旧 Token、groups 或 fallback 放行
- [x] 8.4 将 capability 收敛为 current=`v2`、accepted=`[v2]`、V1 compatibility disabled
- [x] 8.5 删除 Python V1 canonical/verifier 分支、V1-only fixtures 和兼容启用配置
- [x] 8.6 删除 Go V1 signer/canonical 分支与 V1-only vectors，三类调用方只生成 V2
- [x] 8.7 删除 Registry 对 `CENTRAL_ADMIN_SYNC_TOKEN` 的配置、错误和测试依赖，保留仍有明确 ingress 用途的 Knowledge 边界并更新命名说明
- [x] 8.8 删除将 OAC role 投影为 Host groups 的残留 Admin/Coze 路径，确认 V1 groups 不能恢复 Agent 权限
- [x] 8.9 重新运行旧 V1 拒绝、current-only 启动、key rotation 和跨环境 replay 测试
- [x] 8.10 确认代码、环境样例和运行文档不存在可长期重新开启 V1 的隐藏分支或双协议描述

## 9. 文档、追踪与最终门禁

- [x] 9.1 更新 OIR Host Identity/API/Capability 文档，说明 V2 Envelope、三类 profile、认证与授权错误边界
- [x] 9.2 更新 OAC Registry、Knowledge Admin、Coze 接入和环境变量文档，区分 ingress token 与 downstream Host HMAC
- [x] 9.3 编写先验后签、双验签、current-only、回滚冻结和提交未知处置运行手册
- [x] 9.4 在 `replace-irs-with-oir-oac-adapter` 与 `fix-oac-agent-routing-entitlements` 增加本 change 的需求追踪引用，不改写其已完成证据
- [x] 9.5 运行 OAC `apps/server go test ./...`、migration verifier 和相关 Client 静态验证
- [x] 9.6 运行 OIR profile/identity/registry/knowledge/router 聚焦测试、全量 pytest、Ruff check/format 和边界 fitness test
- [x] 9.7 运行 OpenSpec strict validation，确认 proposal/design/spec/tasks 与 current-only 实现及证据一致
- [x] 9.8 最终检查其他 Memory/Turn/Invocation 未提交修改均被保留，且本 change 未修改 OAC workflow/CI 回退文件
- [x] 9.9 输出本地与测试环境验收报告，只有 V1 删除和 current-only 门禁均完成后才允许归档本 change
