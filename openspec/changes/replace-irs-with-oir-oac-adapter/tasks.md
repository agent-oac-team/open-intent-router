## 1. 契约冻结与实施参数固化

- [x] 1.1 启动本地 IRS 并导出 22 个 Central、Registry、Knowledge 方法的 OpenAPI/JSON Schema 快照
- [x] 1.2 为每个 IRS 方法抓取至少一个成功请求/响应 fixture 并脱敏
- [x] 1.3 抓取验证错误、越权、无命中、Provider 失败、超时与截断 warning 的 IRS fixture
- [x] 1.4 对比《OAC 共享知识库接口文档》、IRS 代码与实际 fixture，输出逐接口差异表
- [x] 1.5 固化 OAC 新请求、直接回复、Agent 打开/续接、Plan 确认、Event 回流和失败重试的可执行时序 fixture
- [x] 1.6 建立版本化 Route Golden Dataset，覆盖 8 种 action、Agent 权限、单/多意图、澄清与不支持
- [x] 1.7 建立版本化 Knowledge Golden Dataset，覆盖 Search/Grouped/Read/Assets/Chunks、权限、无命中与空客群槽位
- [x] 1.8 在 OAC 本地/测试拓扑中验证可用的受信代理签名方式，固化 header、audience、timestamp/nonce 和 key rotation 契约
- [x] 1.9 盘点 OAC Go、Next.js 和 Coze 的 Central/Knowledge URL 配置入口并固化本地/测试切换矩阵
- [x] 1.10 固化 `execution_ticket` 在 OAC Route 响应、前端运行态和 Agent Event 中的可选字段位置
- [x] 1.11 根据 IRS 本地基线固化 Route/Knowledge 延迟、超时、Circuit、Diff 与稳定窗口阈值
- [x] 1.12 将上述契约、差异和实施参数固化结果纳入 Adapter contract test fixtures 与迁移记录

## 2. Host Adapter 骨架、路径所有权与身份桥

- [x] 2.1 创建 OAC Host Adapter 的 API、compat schemas、mappers、identity、shadow、fallback 和 repositories 逻辑模块
- [x] 2.2 创建 OAC Host Composition Root，在同进程中显式组装 Adapter 与 OIR Core
- [x] 2.3 实现 Host profile 配置，分离 Core 配置与 OAC/IRS/Coze 专有配置
- [x] 2.4 显式分离 Legacy 公开路径与 OIR Native 内部路径，测试 `knowledge/search` 不按 Body 猜测协议
- [x] 2.5 建立 Adapter 只能调用 OIR 公开应用端口的依赖边界和测试替身
- [x] 2.6 增加 Core 反向 import 和 OAC/IRS/Coze 专有词/字段的 CI fitness test
- [x] 2.7 实现强制 `tenant_id=oac`、受信 user 覆盖、group/attribute 允许列表与 OIR 身份签名
- [x] 2.8 实现 OAC 普通用户、Admin 和 Coze 分离凭证与最小权限策略
- [x] 2.9 补充伪造 tenant/user/tag、过期签名、nonce 重放和 Coze 越权写入测试
- [x] 2.10 实现脱敏 capability/health 端点，输出 Adapter/Core/Schema/Policy 版本与运行模式

## 3. Canonical Conversation Turn 与 Transactional Outbox

- [x] 3.1 定义通用 Canonical Turn、Turn Status、Turn Result References 和 Turn Capsule schemas
- [x] 3.2 增加 Canonical Turn 与 Outbox PostgreSQL 模型、索引、外键和数据库迁移
- [x] 3.3 实现 Turn Repository 的幂等创建、所有权读取、版本更新与终态保护
- [x] 3.4 实现以 `tenant_id/user_id/request_id` 为主键的 Turn 幂等服务和身份冲突检测
- [x] 3.5 在 Route 入口中创建或恢复 Canonical Turn，不把 Host 历史消息副本当作 Turn
- [x] 3.6 实现 `reply/clarify/unsupported/silent` 的 Route-only Turn 完成且不伪造 Run
- [x] 3.7 实现关联 Run/Result/Plan 的活动 Turn 生命周期与终态不可重开约束
- [x] 3.8 实现 Run/Result/Plan/Turn/Outbox 的单事务协调边界
- [x] 3.9 实现 Outbox 投递、claim、重试、dead-letter 与幂等发布
- [x] 3.10 补充 Turn 幂等、跨用户、并发终态、迟到 Event 与事务回滚测试
- [x] 3.11 更新 OIR Native API 文档与数据库文档，说明 Turn 与 Host 展示消息的边界

## 4. Delegated External Run 与 Execution Ticket

- [x] 4.1 定义通用 start/progress/complete/fail/cancel/timeout Delegated Run 命令与应用端口
- [x] 4.2 扩展 Agent Run/Result/Event 模型以支持 delegation、deadline、claim、Turn 关联和状态版本
- [x] 4.3 实现外部副作用前的 Delegated Run 预创建与安全 handoff 返回
- [x] 4.4 实现 progress Event 幂等、所有权、Agent/Plan/Step 关联与顺序校验
- [x] 4.5 实现最终 Event 对 Run/Result/Plan/Turn/Outbox 的原子收口
- [x] 4.6 实现 Run deadline、超时收敛、孤儿检测和受权修复/终止端口
- [x] 4.7 定义并实现不透明 Ticket 签发、hash 存储、所有权/用途/过期/nonce 校验
- [x] 4.8 实现 Ticket claim/lease、Core 提交后 consumed 收敛与崩溃恢复
- [x] 4.9 实现无 Ticket 的受信 request/event/plan/step 唯一映射过渡，零/多命中硬性拒绝
- [x] 4.10 补充 Ticket 伪造、过期、跨用户、重放、重复终态、claim 崩溃与无 Ticket 歧义测试
- [x] 4.11 增加 Ticket 日志/Trace/Diff/Debug 脱敏测试与敏感词扫描

## 5. Central Compat API 与 Schema Mapper

- [x] 5.1 根据冻结 fixture 定义 Central Route、Navigation Event、Agent Event 和 Plan Confirm compat schemas
- [x] 5.2 实现 IRS `user_query/current_agent/history/frontend_context/source/event/plan/step` 到 OIR Native 请求的纯 Mapper
- [x] 5.3 实现 OIR decision/context/next_action/plan/invocation 到 8 种 IRS route action 与旧 Plan 形状的纯 Mapper
- [x] 5.4 实现 IRS HTTP 状态、验证错误、业务错误与用户可见 message 投影
- [x] 5.5 实现 `POST /api/v1/central/route` compat Handler 与 Turn/Delegated Run 集成
- [x] 5.6 实现 navigation event compat Handler 与幂等 Event 写入
- [x] 5.7 实现 agent event compat Handler、Ticket/过渡关联与 `route_required` 响应
- [x] 5.8 实现 plan confirm compat Handler，覆盖重复确认与跨用户拒绝
- [x] 5.9 将可选 `execution_ticket` 加入 compat 响应/Event Schema，验证旧请求和响应仍可解析
- [x] 5.10 为 Central 四类方法运行全量 IRS fixture Contract Tests
- [x] 5.11 补充直接回复、澄清、Agent 续接/切换/退出、Plan、迟到 Event 的 Adapter E2E 测试

## 6. Agent Registry 兼容、主写与迁移

- [x] 6.1 实现 IRS Agent Registry compat schemas 与 `bot_id/route_path/user_tags/keywords` 纯 Mapper
- [x] 6.2 实现 GET/POST/PUT/enabled PATCH/DELETE 五类 Registry compat Handlers
- [x] 6.3 增加 Registry revision/audit PostgreSQL 模型与迁移，记录操作者、来源、时间和前后差异
- [x] 6.4 为 Registry 更新/启停/删除实现乐观版本条件与冲突响应
- [x] 6.5 实现 Registry 单主写启动/readiness 校验，阻止 IRS/飞书/file 反向覆盖
- [x] 6.6 停用 IRS 飞书 Registry 读写、定时同步、恢复配置与相关凭证使用路径
- [x] 6.7 实现 9 个现有 Agent 的幂等导入脚本和稳定 ID 映射
- [x] 6.8 生成并执行逐 Agent 的名称、启用、权限、正/负触发、调用和 UI handoff 对账报告
- [x] 6.9 补充 Registry 管理鉴权、非法 route path、并发更新、删除后不恢复与公开视图脱敏测试
- [x] 6.10 更新 Registry API、字段映射、主写源与飞书停用文档

## 7. 通用 Knowledge Asset/Chunk/Read/Admin 能力

- [x] 7.1 定义通用 Knowledge Asset、Chunk、Asset Group、Import Job、Source Ref 和 Migration Manifest schemas
- [x] 7.2 增加 Knowledge canonical PostgreSQL 模型、索引、约束、软删除和数据库迁移
- [x] 7.3 实现 Asset/Chunk/Group/Job/Manifest Repositories 与基于 tenant/owner/status 的受权查询
- [x] 7.4 实现通用 Knowledge Search 的 caller/purpose/scope/policy/budget 处理与 evidence provenance
- [x] 7.5 实现 Grouped Search 的稳定顺序、独立 trace/warnings 和 include-empty 资产槽位
- [x] 7.6 实现 asset/assets/chunk/chunks/source_ref 五种 Exact Read target 与顺序/分页/缺失/过滤/歧义语义
- [x] 7.7 验证 Exact Read 仅使用 PostgreSQL canonical data，Milvus 不可用时仍能正确授权读取
- [x] 7.8 实现文件扩展名、大小、content type、文件名、哈希和解析资源限制校验
- [x] 7.9 实现 uploaded/processing/indexed/failed/disabled/deleted 与 parsing/chunking/embedding/indexing/cleanup 入库状态机
- [x] 7.10 实现同步 Asset + Job 应用返回、持久化 Job、replace、retry、soft-delete 与 cleanup warning
- [x] 7.11 实现知识检索/管理 Trace，记录 policy outcome/evidence/warnings/latency 且执行正文脱敏
- [x] 7.12 补充 Asset/Chunk 权限、secret/disabled/deleted/failed 过滤、Exact Read、Grouped Search 与入库失败测试

## 8. Knowledge IRS Compat Facade

- [x] 8.1 根据冻结基线定义 Search/Grouped/Read/Assets/Chunks 兼容请求响应 Schema
- [x] 8.2 根据冻结基线定义 Knowledge Admin 上传/列表/详情/删除/Chunks/Retry 兼容 Schema
- [x] 8.3 实现 Search `filters/scope`、consumer/purpose、`return_options`、预算和 evidence 的双向 Mapper
- [x] 8.4 实现 `matched/confidence/evidence/warnings/trace_id` 与 HTTP 200 Provider 降级语义
- [x] 8.5 实现 `content_production` 与 `01`~`06` 稳定键/ID 的 Grouped Search 投影
- [x] 8.6 实现 Exact Read 五种 target、批量顺序、pagination、missing/filtered/ambiguous warnings 投影
- [x] 8.7 实现 Assets/Chunks 列表与详情 compat Handlers 及每个目标的权限过滤
- [x] 8.8 实现 Knowledge Admin compat Handlers，保持同步 Asset + Job、replace/retry/delete/cleanup 语义
- [x] 8.9 将 OAC Admin 与 Coze/Agent/central 查询凭证分离应用到所有 Knowledge Handlers
- [x] 8.10 对全部 Knowledge 查询/读取/管理 fixture 运行 IRS/OIR Contract Tests
- [x] 8.11 补充 Provider warning、空客群槽位、超大文件、解析失败、重试和幽灵证据清理测试

## 9. 原始知识数据重建

- [x] 9.1 新建 OIR 独立 PostgreSQL database 初始化与迁移脚本，验证不写入 IRS/OAC `oac` database
- [x] 9.2 配置独立 `oir_knowledge_vectors` 和 `oir_memory_vectors` collections，验证 collection 不串用
- [x] 9.3 实现 Excel 多级表头、换行、长文本、空列与 source sheet/row/range 的确定性 Parser
- [x] 9.4 实现原文保留、规范化结果、file/content hash 和 parser/chunking/embedding 版本记录
- [x] 9.5 实现以 file hash + pipeline version 为键的幂等导入和 Manifest 状态迁移
- [x] 9.6 导入 `01 要素表.xlsx` 并验证分类、source_ref、Chunk 与向量对账
- [x] 9.7 导入 `02 KPI + 场景.xlsx` 并验证 KPI/场景/子场景结构与 Golden Queries
- [x] 9.8 导入 `03 客群表.xlsx`，标记 empty/deferred，保留 `03` 槽位且不生成 Chunk/向量
- [x] 9.9 导入 `04 活动表.xlsx` 并验证长正文、时效、条件与来源
- [x] 9.10 导入 `05 权益表.xlsx` 并验证长文切块、门槛、领取、有效期与精确读取
- [x] 9.11 导入 `06 企微模板.xlsx` 并验证模板结构、公式、样例与精确读取
- [x] 9.12 运行全量 Golden Queries、Exact Read、业务字段、source_ref 和权限负向验收
- [x] 9.13 验证 `oir_knowledge_vectors` 全部来自 OIR canonical Chunk 重建，无 IRS 旧向量复制
- [x] 9.14 生成 6 份文件的 Migration Manifest 与 parsed/indexed/validated/deferred 最终报告

## 10. Canonical Turn 驱动的 Context 与 Memory

- [x] 10.1 实现从已完成 Canonical Turn 构造所有权可验证 Turn Capsule
- [x] 10.2 将 Memory Formation 触发改为消费 Turn Outbox，拒绝孤立 Message、Event、部分 Run/Plan 输入
- [x] 10.3 实现 turn ID + policy version + formation window 的 Formation 幂等与重复投递收敛
- [x] 10.4 配置 Decision Shadow 无 Memory 副作与 State Rehearsal 独立 database/schema/collection 写入
- [x] 10.5 实现 Formation、Recall 和 Worker 独立 mode-off，保留 canonical ledger 与可观测跳过原因
- [x] 10.6 验证切换新 Memory 数据域不导入 IRS/OAC 旧历史消息或运行态
- [x] 10.7 补充完成/非终态/孤儿 Turn、重复 Outbox、Shadow 隔离、mode-off 和跨用户 Memory 测试
- [x] 10.8 更新 Memory Formation、隔离、修复、mode-off 与切换运维文档

## 11. OAC 最小兼容改造

- [x] 11.1 更新 OAC 共享 API 类型，在 Central Route 与 Agent Event 中加入可选不透明 `execution_ticket`
- [x] 11.2 修改 AI Sidebar 运行态以原样保存当前 Ticket 并在对应 Agent Event 中回传
- [x] 11.3 修改 OAC Go Central Proxy 以不解析、不记录、不丢失 Ticket 地转发可选字段
- [x] 11.4 验证 OAC Go `CENTRAL_API_BASE_URL` 可在本地/测试切到 Adapter 且不破坏非 Central API
- [x] 11.5 验证 OAC Next.js 精确读取和 Go Knowledge Admin 代理均使用 Adapter 目标地址
- [x] 11.6 配置 Coze Workflow Knowledge Base URL 指向 Adapter，不改变 Workflow 响应后处理
- [x] 11.7 补充 OAC TypeScript/Go 的 Ticket 向后兼容、丢失防护、脱敏与 Central/Knowledge 代理测试
- [x] 11.8 明确禁止导入或恢复 OAC/IRS 历史消息的启动/迁移配置与测试

## 12. Replay Shadow、Diff、Circuit 与 Write Fence

- [x] 12.1 定义 `read_only/route_stateful/control_write/runtime_write` 操作分类并为每个 Adapter 方法静态标注
- [x] 12.2 实现启动/readiness Write Fence，阻止写操作双主配置与所有写 fallback
- [x] 12.3 实现 Route 提交状态幂等查询，支持证明 `not_accepted`、committed 与 unknown
- [x] 12.4 实现可配置 closed/open/half-open Circuit Breaker、探测与恢复条件
- [x] 12.5 实现只读 Knowledge 与可证明未提交 Route 的 IRS Fallback Gateway
- [x] 12.6 实现 timeout + ambiguous commit 的 fail-closed 响应与 `fallback_blocked` 审计
- [x] 12.7 增加 Shadow Replay Dataset 版本、样本覆盖率、IRS/OIR 结果与 Diff PostgreSQL 模型
- [x] 12.8 实现全量 Route/Knowledge/Permission/E2E replay runner 与可重复报告输出
- [x] 12.9 实现 Decision Shadow 无持久副作用模式，拦截外部 Agent、页面动作和所有可召回写入
- [x] 12.10 实现 State Rehearsal 独立 database/schema/Milvus 配置校验和误指主数据域的启动拒绝
- [x] 12.11 实现 Route 结构 Diff 与 Knowledge 结构/permission/warning Diff
- [x] 12.12 实现行为指纹、严重度、批准白名单和 blocking 门禁
- [x] 12.13 增加 Adapter/OIR/IRS 流量、延迟、Diff、Fallback、Write Fence、Turn/Run/Memory 指标与关联 Trace
- [x] 12.14 补充 Circuit、提交证明、unknown timeout、写阻止、Shadow 副作用拦截与隔离误配测试

## 13. 本地完整联调与回归

- [x] 13.1 准备 OIR PostgreSQL、Knowledge/Memory Milvus、IRS 对照和 OAC 本地 Host Runtime 的非敏感配置模板
- [x] 13.2 运行 Central、Registry 和 Knowledge 全量 Contract Tests 并修复所有未批准差异
- [x] 13.3 在 OAC AI Sidebar 验证直接回复、澄清、8 种 action、Agent 切换/退出与 Plan 确认
- [x] 13.4 验证 OAC 外部 Agent 的 Ticket、progress、final Result、Plan Step、Turn 与 Outbox 端到端对账
- [x] 13.5 验证重复/伪造/过期 Ticket、迟到 Event、超时/孤儿 Run 和事务回滚路径
- [x] 13.6 验证 OAC Knowledge Admin 上传/列表/详情/删除/重试与 Next.js Exact Read 链路
- [x] 13.7 验证 Coze consumer 的全量 Knowledge Contract 与传输级 smoke，不将 Workflow 后处理纳入门禁
- [x] 13.8 启用 Governed Context、Memory Recall 与 Formation，验证完整 Turn 形成、跨用户隔离与长期记忆治理
- [x] 13.9 演练 Memory mode-off、Outbox/Index/Formation dead-letter、repair 和孤儿 Run 清理
- [x] 13.10 输出本地 E2E、契约、权限、数据对账与故障演练报告

## 14. 测试环境 100% Shadow 与干净切换

- [x] 14.1 在测试环境部署 OAC Host Runtime、OIR 独立 database/collections 与隔离 State Rehearsal 数据域
- [ ] 14.2 对版本化 replay dataset 执行 100% Decision Shadow 并生成覆盖率证据
- [ ] 14.3 执行隔离 State Rehearsal，对账 Turn/Run/Result/Plan/Event/Outbox/Memory 且验证主数据域无副作用
- [ ] 14.4 处理或批准 Route/Knowledge/Latency Diff，清零权限放宽、跨用户、核心事实矛盾和重复写 blocking Diff
- [ ] 14.5 演练 Circuit open/half-open/recovery、只读 fallback、安全 Route fallback、unknown timeout 和写阻止
- [ ] 14.6 启用 IRS 新运行态/控制面写入冻结，禁止新 Session/Plan/Event/Registry/Knowledge Admin 写入
- [ ] 14.7 实现并执行 IRS 活动 Plan、在途 Agent 和待回调 Event 盘点/排空脚本
- [ ] 14.8 完成、取消或显式终止所有 IRS 活动运行态，生成零活动对象排空报告
- [ ] 14.9 生成 cutover watermark、契约/非敏感配置快照，确认不保留或导入历史消息正文/运行态
- [ ] 14.10 将 OIR 切为 Central、Registry、Knowledge、Turn/Run/Plan/Event/Memory 唯一事实源
- [ ] 14.11 将 OAC Go/Next.js 和 Coze Knowledge URL 切换到 Adapter 并执行 OAC E2E/Coze transport smoke
- [x] 14.12 实现并验证 cutover watermark 之前迟到 IRS Event 只进入脱敏隔离审计
- [ ] 14.13 在稳定窗口演练冻结 OIR 写入、保存 watermark、只读回退与未完成 Delegated Run 终止
- [ ] 14.14 验证 IRS 无活跃请求、无活动运行态、无未处置迟到回调和无未知直连消费者
- [ ] 14.15 移除 IRS fallback，停用 IRS 服务与飞书 Registry 同步，清理不需保留的 IRS/OAC 历史消息和运行态数据
- [ ] 14.16 输出测试环境 Shadow、Diff、Circuit、Write Fence、Drain、Cutover、Smoke 与 IRS 下线报告

## 15. 文档、CI 与最终验收

- [x] 15.1 更新 OIR `docs/api.md`、数据模型、Host Adapter 边界、Identity、Ticket 和 capability 文档
- [x] 15.2 生成 Legacy/Native API 兼容矩阵、JSON Schema 快照索引与变更记录
- [x] 15.3 编写本地/测试启动、Shadow Replay、State Rehearsal、Circuit/Write Fence 与故障处置 Runbook
- [x] 15.4 编写 IRS 冻结、排空、cutover watermark、迟到回调隔离、回滚和下线 Runbook
- [x] 15.5 更新 `.env.example` 和配置文档，只使用占位值且不暴露 Token、Ticket、数据库密码或真实凭证
- [x] 15.6 将 Adapter/Core 依赖方向、专有词、Schema 快照和变更 OpenSpec 校验加入 CI
- [x] 15.7 运行 OIR `.venv/bin/python -m pytest` 并修复所有回归
- [x] 15.8 运行 OIR `.venv/bin/python -m ruff check .` 与 `.venv/bin/python -m ruff format --check .`
- [x] 15.9 如修改 OIR Web，运行 `web` 的 `npm run test` 与 `npm run build`
- [x] 15.10 运行 OAC 受影响 TypeScript/Go 校验、`go test ./...` 与相关 Client build
- [x] 15.11 运行 `openspec validate replace-irs-with-oir-oac-adapter --strict`
- [ ] 15.12 按需求追踪矩阵对账所有 P0 需求、测试证据、迁移报告和 Definition of Done
