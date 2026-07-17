## Why

OAC 当前依赖 IRS 提供中控路由、Agent Registry、共享知识库和 Plan/Event 运行态，而 OIR 已具备更通用的 Router、Plan、Context、Memory 和 Knowledge 基础，但两者契约不兼容。OAC 已停止运营且处于开发状态，当前是通过干净切换将 OIR 建立为唯一事实源、下线 IRS，同时保持 OIR Core 通用性的最低风险时机。

## What Changes

- 在 OIR 同仓、同进程内建设逻辑独立的 OAC–OIR Host Adapter，对外保持 IRS Central、Registry 和 Knowledge 契约，对内只依赖 OIR 公开应用端口。
- 建设通用 Canonical Conversation Turn 和 Delegated External Run，由 OIR 统一收口 Turn/Run/Result/Plan/Event 不变量，Adapter 只做协议、身份和不透明 Ticket 传输。
- 补齐 OIR 通用知识 Asset、Chunk、Search、Grouped Search、Exact Read 和 Admin 入库能力，Adapter 实现《OAC 共享知识库接口文档》的完整兼容面。
- 以 `/Users/lijingtong/project/data/内容生产` 的 6 份原始工作簿重新解析并建立 PostgreSQL canonical knowledge 和独立 `oir_knowledge_vectors`，不复制 IRS 向量。
- 将 OIR Agent Registry 设为唯一主写方，迁移并验证现有 Agent 定义，停用飞书 Registry 同步。
- 在无真实用户流量的开发态环境中，对全量契约样本、Golden Dataset 和合成 E2E 执行 100% Decision Shadow 与隔离 State Rehearsal。
- 对 Route 和只读 Knowledge 实施有提交证明的安全熔断回退；对 Registry、Knowledge Admin、Event、Plan 和 Memory 写入实施硬性 Write Fence。
- **BREAKING（存量状态）**：IRS 的 Session、Message、Event、Plan、Result 和历史消息不迁移、不继续保留；切流前排空或终止所有活动运行态，切流后 OIR 从空运行态开始。
- OAC 仅做必要的向后兼容改造，包括 Adapter 地址配置和可选 `execution_ticket` 透传；Coze Workflow 只验证知识接口传输与响应契约。

## Capabilities

### New Capabilities

- `oac-irs-compat-adapter`: OAC/Coze 对 IRS Central、Registry、Knowledge 契约的兼容入口、身份桥、Schema 映射与 Core 依赖边界。
- `canonical-conversation-turn`: 完整会话轮次的创建、完成、幂等、所有权、Result 关联与 Memory Formation 触发不变量。
- `delegated-external-run`: 宿主执行外部 Agent 时的预创建 Run、不透明 Ticket、回调关联、超时/重放防护与原子收口。
- `knowledge-asset-management`: 通用 Knowledge Asset/Chunk 的入库状态机、分组检索、精确读取、权限、provenance 和管理契约。
- `migration-shadow-cutover`: 回放式 Shadow Diff、State Rehearsal、Circuit Breaker、Write Fence、干净切换、迟到回调隔离与 IRS 下线门禁。

### Modified Capabilities

- `agent-registry`: 增加主写事实源、版本/审计、兼容映射和飞书同步停用要求。
- `knowledge-vector-transition`: 将过渡规则收敛为从原始文件重建 OIR canonical chunks 和独立向量索引，并以 Migration Manifest 完成切换审计。
- `memory-context-governance`: 将自动 Memory Formation 的可信输入收紧为已完成且所有权可验证的 Canonical Turn，并要求 Shadow 写入隔离与 mode-off。

## Impact

- OIR：`app/api`、`app/schemas`、Router/Plan/Invocation/Context/Memory/Knowledge Services、Repositories、数据库模型、迁移脚本、配置、可观测与测试。
- 新 Host Adapter：OAC 专用 Compat Schema/Mapper、Identity Bridge、Ticket 传输、Shadow/Fallback 策略和 Composition Root，但不得被 OIR Core 反向依赖。
- OAC：Central/Knowledge Base URL 配置、Go 代理、AI Sidebar 可选 Ticket 透传和相应契约测试。
- IRS：契约基线与回放对照源；切流前冻结和排空，只保留契约/配置快照、排空报告与 cutover watermark，不保留历史消息或运行态数据。
- 数据：新建 OIR PostgreSQL database、独立 Knowledge/Memory Milvus collections、Migration Manifest、Shadow Diff 与 cutover watermark。
- 外部系统：Coze Workflow 继续使用 IRS 兼容知识契约，但请求终点改为 Adapter；不验收 Workflow 后续处理。
