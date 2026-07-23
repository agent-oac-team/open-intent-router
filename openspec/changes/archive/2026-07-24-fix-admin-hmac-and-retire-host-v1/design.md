## Context

OAC 到 OIR Host Adapter 已有一套跨语言 HMAC signer/verifier：V1 为固定 principal/groups Envelope，V2 在同一公共请求绑定字段后追加 roles、active Bundle、claims version 和 policy version。当前 Agent Route 使用 V2；Admin 和 Coze 被设计为迁移期 V1 credential class。

本地真实链路暴露了协议实施断层：OAC Go 的 Registry 客户端 `centralAdminRegistryRequest` 仍只发送 `X-Admin-Sync-Token`，未调用 Host signer；OIR Registry Handler 则只接受 `get_trusted_host_identity` 产生的 `oac_admin` 身份。因此 Registry 列表返回 401，经 Go 投影为 502，OAC Client 退回静态 Agent map。Go 单测只断言旧 Token，OIR Registry 测试直接注入 `TrustedHostIdentity`，两侧均未覆盖真实传输。

另一个潜在问题是现有 Knowledge Admin signer 将 OAC role 放入 V1 groups，而 OIR Host 的 legacy groups allowlist 默认空。Admin 控制能力已由 credential class 授予，继续依赖 groups 会让合法请求受无关配置影响。

本变更跨越 OAC Go、OIR Host Identity、Registry、Knowledge、Capability、部署门禁和跨进程测试。OAC 已停止运营且尚无生产环境，适合在本地恢复绿色基线后立即完成协议统一，不把 V1 带入长期运行态。

## Goals / Non-Goals

**Goals:**

- 先恢复 Registry/Knowledge Admin 的真实 actor HMAC 链路，使 OIR Registry 重新成为可用的唯一事实源。
- 保持既有 Route V2 canonical 和冻结向量兼容，把 V2 扩展为 User/Admin/Coze 共用的唯一 current Envelope。
- 用 credential profile 明确每类身份的 principal、claims/policy version、必填/禁止字段和操作权限。
- 提供双验签、版本计数、切换门禁和确定的 V1 删除路径。
- 用真实 OAC Go -> OIR Verifier -> Handler -> Repository 测试关闭当前测试盲区。
- 保持 Adapter 到 Core 的单向依赖，不在 OIR Core 引入 OAC、IRS、Coze 或签名专有字段。

**Non-Goals:**

- 不改变 OAC Client、Coze Workflow 所见的 IRS-compatible API Schema 或返回后处理。
- 不迁移或清理 Agent definition、历史消息、Turn、Run、Plan、Memory、Knowledge 内容。
- 不扩展 entitlement 粒度、Bundle 规则、Agent invocation 类型或会话生命周期。
- 不为 Registry/Knowledge 写操作增加 IRS fallback、双写、自动重放或补偿式回滚。
- 不在本变更中建设多副本共享 nonce store；首次本地/测试部署保持单一 OIR Host 实例，水平扩容前另行建设原子共享 replay store。

## Decisions

### 1. 先做有界 V1 修复，再完成协议统一

第一阶段只让现有 Registry 客户端调用已存在的 signer，恢复控制面绿色基线；不新增 V1 schema、抽象、业务分支或长期配置。修复完成后立即进入 V2 profile 迁移，V1 明确标记 `migration compatibility only`。

不选择“在 Registry 失效时直接大爆炸切换全部协议”，因为这会把单一漏签问题与三类 credential 重构混在一起，失败时无法区分控制面、canonical、profile 或部署顺序问题。也不选择让 Adapter 接受旧 Token，因为 bearer token 不绑定 path/body、没有 nonce/replay 防护，并会形成第二授权事实源。

### 2. 统一版本使用扩展后的 `OIR-HOST-V2`，不新建 V3

V2 现有 canonical 行顺序保持不变：公共 Envelope 后继续包含 roles、groups、active Bundle、claims version、policy version 和 credential class。扩展只发生在 Verifier 的 profile 规则，不更改现有 `oac_user` Route 向量含义，因此既有 Go/Python V2 frozen vector 必须继续逐字节匹配。

选择扩展 V2 而非创建 V3，是因为固定 canonical 已能表达空 roles/groups/Bundle 和非空 profile versions；新增 Admin/Coze 只是增加合法 profile 组合。若修改任何 canonical 字段、顺序或编码规则，则必须停止本方案并发布新协议版本，禁止在 V2 名下静默改变签名文本。

### 3. 三类 credential 使用显式 profile

| Credential class | Principal | Claims version | Policy version | Roles | Groups | Active Bundle | Operations |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `oac_user` | `user` | `oac-principal-v1` | `oac-authz-v1` | 必填 | 禁止 | 必填 | read、route、own runtime write |
| `oac_admin` | `user` | `oac-admin-principal-v1` | `oac-control-v1` | 禁止 | 禁止 | 禁止 | read、control write |
| `coze_workflow` | `service` | `oac-service-principal-v1` | `oac-readonly-v1` | 禁止 | 禁止 | 禁止 | Knowledge read only |

Verifier 先根据 key ID 确认允许的 credential class，再按 profile 精确校验 principal type、claims/policy version 和字段集合。未知字段组合、跨类 key、额外 roles/groups/Bundle 都按认证失败处理；操作不在 profile policy 内才返回授权失败。

### 4. Admin key 证明控制面来源，subject 只表示真实 actor

Registry 与 Knowledge Admin 共用一个 Go identity builder：current Admin key、`principal_type=user`、真实 OAC user ID、`credential_class=oac_admin`，不携带 role/groups/Bundle。OAC Go 在签名前查询当前用户 role/status/approval；OIR 不再次解释 OAC role，只使用 credential class 决定控制面能力，并把 subject 写入 Registry audit。

权限矩阵保持现有产品行为：普通活跃用户可读取前端所需 enabled Registry；operator/admin 可读取管理列表和使用已有 Knowledge Admin 权限；Registry create/update/enable/delete 仍只允许当前 admin。所有写权限在 Go 侧基于当前数据库事实判断，不使用 JWT 旧 role。

不使用固定 `oac-admin-service` subject，因为它会让所有 Registry revision 丢失真实操作者；不把 role 签进 groups，因为 credential policy 已表达能力，groups 会引入默认空 allowlist 的额外失败条件。

### 5. 旧同步 Token 仅保留 wire compatibility，不参与授权

第一阶段 OAC Go 可继续附带 `X-Admin-Sync-Token`，以保持冻结的 IRS-compatible Header 形状，但 OIR Adapter 不读取它、不放入 Principal、不因其正确而授权。Registry Go 客户端不再因缺少该 Token 而拒绝生成 HMAC；V1 清理阶段删除 Registry 对 `CENTRAL_ADMIN_SYNC_TOKEN` 的配置和测试依赖。

Knowledge BFF 到 OAC Go 的入口认证若仍使用同名 Token，属于不同信任边界，可在其入口保留；Go 到 OIR Adapter 的最终授权始终只有 Host HMAC，文档必须区分 ingress token 与 downstream Host credential。

### 6. Go 在最终请求构造完成后签名

Registry helper 接收 request context 和服务端认证后的 actor，先固定最终 URL、规范化 query 和 JSON Body 字节，再调用共享 signer。Signer 读取并恢复 Body、计算 SHA-256、生成 128-bit nonce 和 timestamp，随后写入 Host headers。调用方不得在签名后修改 method/path/query/body，也不得转发浏览器提供的 `X-OIR-Host-*` Header。

GET/DELETE 使用空 Body hash；包含转义 Agent ID 的 PUT/PATCH/DELETE 必须以 `EscapedPath` 参与 canonical。HTTP client 使用原请求 context，使浏览器断开和服务停机能够取消在途读取，但控制面写入超时后仍按提交未知 fail closed。

### 7. 失败语义区分认证故障和业务冲突

- Go signer 缺少 current key、actor 或 profile：请求不得发出，返回服务不可用并记录脱敏配置错误。
- OIR 401/403：Go 不向浏览器暴露 key/profile 细节，记录 operation、credential class、signature version 和 request correlation，返回受控内部依赖错误。
- OIR Registry 404/409/422：Go 保留可操作的业务状态与兼容消息，不统一误报为 502。
- Registry/Knowledge 写超时或连接断开且提交状态不明：禁止 fallback 和自动重试；管理员需先按 stable ID/revision 查询事实源再决定动作。

日志不得输出 HMAC、key secret、旧同步 Token、execution ticket、完整 claims 或不必要 Body。

### 8. Capability、计数和关闭开关共同构成迁移门禁

迁移窗口 capability 输出：`current_signature_version=v2`、`accepted_signature_versions=[v1,v2]`、`v1_compatibility_enabled=true`，以及 credential catalog 健康状态。版本使用计数按 signature version、credential class、operation 聚合，不含 subject、Agent ID、正文或 key ID。

Adapter 先支持三类 V2 profile，再逐一切 signer：Agent Route 已为 V2；Registry/Knowledge Admin 切 V2；Coze read 切 V2。只有本地和测试 replay 均证明 V1 计数为零、正向矩阵全绿、负向 fail closed，才能关闭 V1。

关闭后 capability 只报告 V2，Verifier 拒绝 `v1=`。完成一个关闭验证周期后删除 V1 canonical、Go signer 分支、Python verifier 分支、V1 frozen vectors 和启用开关；最终代码不保留“随时重新打开 V1”的永久分支。

### 9. 测试必须跨越真实身份边界

保留单元测试验证 canonical 和 profile 纯函数，但验收证据必须包含真实 OAC Go HTTP handler/client 到真实 OIR Host Verifier/Handler 的 transport 测试。测试使用隔离数据库和固定 fixture key，不能让上游 Stub 无条件接受 Header，也不能在 Adapter 侧覆写 identity dependency。

Registry E2E 覆盖 create/list/update/enable/disable/delete、revision conflict、真实 actor audit 和使用现有 Bundle 的新 Agent 路由可见性。Knowledge 覆盖 Admin read/write 和 Coze exact read；安全矩阵覆盖旧 Token only、错误 key/audience/profile、跨类 key、篡改、过期、replay、V1 disabled 和非 Admin write。

## Risks / Trade-offs

- [临时 V1 修复形成永久债务] → 不新增 V1 抽象，tasks 将 V2 切换和 V1 删除列为同一 change 的完成条件，测试环境切流门禁要求 current-only。
- [在同一 V2 版本增加 profile 被误认为改变 canonical] → 冻结现有 Route vector；任何 canonical 字节变化强制升级新版本而不是修改 V2。
- [Admin key 代签普通用户读取扩大能力] → Go 只暴露固定 enabled-list handler，写入仍经当前事实 RBAC；Adapter 以 path operation policy 限制能力，浏览器永远不持有 Admin key。
- [真实 actor 来自过期 JWT subject] → subject 只用于定位用户，签名前查询当前用户存在性、role、status 和 approval；Body/Header 不能覆盖。
- [旧 Token 与 HMAC 并存造成双重权威误解] → Adapter 完全忽略旧 Token，capability/docs 标明非授权 Header，V1 清理阶段删除 Registry 依赖。
- [写超时后人工重试造成重复 mutation] → 使用 stable Agent ID、expected revision 和原子 audit；提交未知禁止自动重试，先读取当前 revision。
- [计数泄漏调用方身份] → 只聚合 version/class/operation，不记录 subject、resource 或 key ID，并使用低基数固定标签。
- [单进程 nonce store 不支持水平扩容] → 首期本地/测试保持单实例；扩容前必须另建共享原子 nonce store，不能把多副本部署作为本变更的隐含能力。

## Migration Plan

1. 冻结当前失败证据和跨系统 Registry Admin transport 测试，使 token-only 请求稳定复现 401/502。
2. 在 OAC Go 增加 request-scoped Admin actor、当前用户事实校验和共享 Admin identity；以 V1 签名恢复 Registry 五类操作并修正 Knowledge Admin 空 groups，完成本地绿色基线。
3. 在 OIR Adapter 增加 V2 Admin/Coze profiles、严格字段校验、capability 和版本计数；保持 V1/V2 双验签，部署/启动时验证 current keys/profile catalog。
4. 将 Registry/Knowledge Admin signer 切换为 V2，再将 Coze signer 切换为 V2；Agent Route 保持既有 V2。逐类运行正向、越权、篡改、replay 和真实 transport replay。
5. 在隔离本地数据库完成临时 Agent 全生命周期、真实 actor audit、Knowledge Admin 和 Coze Exact Read；确认 OAC Client 不出现 Registry fallback warning。
6. 在测试环境执行 100% replay，要求所有 V1 使用计数为零；测试环境首次正式切流优先直接启用 current-only。
7. 关闭 V1 acceptance，复跑完整回归和旧 V1 拒绝测试；随后删除 V1 canonical、signer/verifier 分支、fixtures、配置和 Registry 旧 Token 依赖。
8. 更新 Capability、运行手册、密钥轮换、故障排查和原迁移 change 追踪关系；change 只有在 current-only 证据完成后才能归档。

回滚分阶段处理：V1 acceptance 尚未关闭时，可仅回滚单个 Go signer 到已验证的 V1，同时 Adapter 保持双验签；不得切回 IRS Registry 或双写。关闭 V1 后若 current 协议出现阻塞，回滚整个发布到上一组双验签版本并冻结控制面写入，不能在运行时通过隐藏开关长期恢复 V1。任何提交未知写入先读取 OIR Registry revision/audit 再处置。

## Open Questions

无。V1 的迁移属性、三项 Admin identity 决策、先修复后统一的顺序以及最终 current-only 目标均已确认。
