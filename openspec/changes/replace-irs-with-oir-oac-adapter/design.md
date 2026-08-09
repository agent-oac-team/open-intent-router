## Context

旧 IRS 同时包含两类不同生命周期：Central/Registry/Plan/Event 等中控能力，以及
Knowledge Search/Read/Admin、文件解析、索引和审计能力。OIR 已经实现通用路由、编排、
Canonical Turn、Delegated Run、Context 和 Memory；OAC 与 Coze 则仍在使用旧 Knowledge
HTTP 契约。OIR Knowledge HTTP 没有已登记调用方，OIR 与旧 IRS 中的知识来自同一来源，
但分别产生 269 与 270 个 Chunk。

继续让 OIR 重建或代理 Knowledge 会制造双事实源，也会把开源路由核心绑定到特定知识
产品。另一方面，OIR Memory 使用向量数据库是独立需求，不能因为外置 Knowledge 而删除
或重建 Memory 数据。

本设计以 `CONTEXT.md` 的领域语言和父规格 #2 为准。原 IRS 的 Knowledge 部分正式成为
独立产品 `knowledge_sys`；“IRS”只描述待退役的历史中控。

## Goals / Non-Goals

**Goals:**

- OIR 成为 Central Route、Registry、Turn、Run/Result、Plan/Event、Context 和 Memory 的唯一事实源。
- `knowledge_sys` 成为 Knowledge Asset、Chunk、Search、Read、Admin、ACL、索引、缓存和审计的唯一事实源。
- OIR 与 `knowledge_sys` 可独立部署；未配置 Knowledge Provider 时 OIR 仍能路由和编排。
- OIR Core 只依赖相互独立、Provider-neutral 的 Evidence Provider 和 Knowledge Provider。
- 保持 Agent Definition 的通用 Knowledge 声明和 Invocation `knowledge_context`，同时确保知识正文瞬时化。
- 清理 OIR Knowledge 副本且不迁移、重建或重新 Embedding Memory。
- 在同一迁移计划内追踪改名、鉴权、清理、Central Retirement Gate、测试/生产切换和旧入口退役。

**Non-Goals:**

- 不把 OIR 的 269 个 Chunk 或向量复制、合并到 `knowledge_sys`，也不抹平 269/270 差异。
- 不修改 SQL 数据库名、Milvus 数据库/Collection 名或 PostgreSQL 角色 `oac`。
- 不在 OIR 保留 Knowledge Search、Read、Assets、Chunks、Admin 或 HTTP Proxy。
- 不让 Knowledge Provider 参与 Route Decision，或让 Evidence Provider承担 Agent Knowledge。
- 不把 Provider ID、URL、`knowledge_sys` 或 IRS 名称写入 Agent Definition 或 OIR Core。
- 不将外部 Knowledge Context 自动写入 Memory。
- 不为 IRS 中控保留兼容转发，也不在退役后回滚到 IRS。

## Decisions

### 1. 两个事实源独立部署

OIR 独占中控运行事实；`knowledge_sys` 独占组织知识事实。二者使用独立进程、部署配置、
健康检查和发布生命周期。OIR 的正常启动、路由和编排不要求部署 `knowledge_sys`。

`knowledge_sys` 使用正式仓库名 `knowledge_sys`、distribution/服务名 `knowledge-sys`、
测试服务名 `knowledge-sys-test`，部署目录为 `/var/www/knowledge-sys[-test]`。产品改名
不触发存储迁移：现有 SQL 数据库 `oac`、Milvus Collection `oac_knowledge_chunks` 和
PostgreSQL 角色 `oac` 均作为 Storage Identifier 保留。

### 2. Evidence 与 Knowledge 使用两个窄端口

Evidence Provider 在 Route Decision 前提供 Evidence、意图提示或固定问命中。Knowledge
Provider 只在 Agent Context 阶段执行单一异步操作：

```text
retrieve(request) -> result
```

请求包含受信 Principal、Query、Purpose、Consumer、逻辑 Source Key/Tag 和预算；结果包含
状态、Items、Citation、Provider Trace、Warnings、稳定错误码与截断信息。Provider 不能
返回 Route Override、候选 Agent 或目标 Agent。Provider 实现由部署和租户策略解析，Agent
Definition 不声明实现名称或地址；未来多后端由端口后的 Composite Provider 实现。

OIR Core 不镜像 `knowledge_sys` 的 Search、Grouped Search、Exact Read、Assets、Chunks 或
Admin HTTP 契约。面向 `knowledge_sys` 的 JWT、HTTP 和错误映射只存在于 Provider Adapter。

### 3. Knowledge Requirement、Mode 与 Context Handle

`AgentDefinition.context.knowledge` 保持 Provider-neutral，并增加 `optional|required`，
默认 `optional`。`disabled` 只允许 optional；`prefetch` 支持两种 Requirement；
`controlled_retrieval + optional` 只在显式工作流节点检索。

`controlled_retrieval + required` 必须在 Invocation 前消费 OIR 签发的短时 Knowledge
Context Handle。Handle 绑定 tenant、principal、Agent、Source Scope 与 trace，调用方提交的
任意 `knowledge_context` 不能作为受信检索证明。

required 在治理后没有可用项时返回 `knowledge_not_found`；Provider 缺失、拒绝、超时或
故障时返回 `knowledge_unavailable` 并阻止 Agent 调用。optional 则携带结构化状态继续。

### 4. Knowledge 正文瞬时化

Agent 最终继续接收 `knowledge_context`。正文只存在于当前 Invocation 的内存载荷；
异步交付确有需要时，只能进入加密、短 TTL 临时存储并在终态清除。OIR 不建立跨请求
Knowledge Cache，也不在 Run、Event、Trace 或日志持久化正文。

长期记录只保存 Citation、Provider `trace_id`、`item_id`、数量、状态和错误码。Citation
最小只要求逻辑 `source_id`；审计定位使用结果级 `trace_id` 与条目级 `item_id`。
`knowledge_sys` 在默认 30 天、可配置的保留期内解析引用，读取时重新鉴权；删除或收权后
可返回 Tombstone，过期返回 `trace_expired`。

Knowledge Context 不是 Memory Formation 证据。单请求内可对完全相同的检索去重；跨请求
缓存只归 `knowledge_sys` 所有。

### 5. Memory Store 与 Knowledge 完全解耦

Memory 仍由 OIR 私有管理并可使用向量数据库。配置解耦分两步完成：

1. 将当前有效 Memory Store、Embedding 和 Collection 值写入完整的 `MEMORY_*` 配置，
   记录 Collection、数量、读取、Recall、Create、Update、Delete 基线。
2. 删除所有 `KNOWLEDGE_*` 回退、Knowledge Transition Collection 元数据和交叉校验；
   Memory 启用而显式配置缺失时启动失败。

该过程不重建、不重新 Embedding、不迁移 Memory，也不创建新的隐式 Collection。

### 6. OAC 与 OIR 分别通过正式身份链访问 Knowledge

正式 `/knowledge-api` 只接受 HTTPS Bearer JWT。OAC 和 OIR 各自持有签发私钥，
`knowledge_sys` 仅配置允许列表中的 Issuer JWKS。JWT 最长 5 分钟，包含 `iss`、`sub`、
`aud=knowledge_sys`、`iat`、`exp`、`jti`、`tenant_id`、`principal_type` 和 `scopes`；
最小 Scope 为 `knowledge:read` 与 `knowledge:admin`。

OAC 浏览器继续调用 OAC Backend，Coze 继续调用 OAC 只读代理；OAC Backend 换发短时 JWT
后直连 `knowledge_sys`。OIR Provider Adapter 直连 `knowledge_sys`。Knowledge Base URL
必须显式配置，不能回退 Central Base URL。

旧 `/central-api` Knowledge 入口仅在边缘验证旧凭据并换发 JWT；旧凭据不进入
`knowledge_sys` Core。所有登记调用方切换并连续 7 天零旧 Knowledge 流量后删除该入口。

### 7. OIR Knowledge 副本只清理、不迁移

`knowledge_sys` 当前 270 Chunk 是唯一 Canonical Data。OIR 的 269 Chunk 只用于清理前
对账，不复制或合并。清理工具必须先生成不含正文的 Knowledge Data Manifest，内容限制为
表名、Schema 版本、数量和聚合 Hash。

Dry Run 遇到未知 JSON 结构必须停止。执行阶段删除 OIR 九类 Knowledge 表、专属
`oir_knowledge_vectors`、本地 Knowledge 向量文件，并定向清除既有 Run/Event/Trace JSON
中的 Knowledge Context 正文。Agent Definition 的 Provider-neutral 配置和全部 Memory
表、Collection、向量、数据都明确排除。

### 8. IRS 中控按独立门禁退役

飞书 Agent Registry Sync 及相关配置直接删除。其余 IRS 中控模块只有在以下证据齐备后
才删除：

- OIR 的 Central、Registry、Agent、Session、Event、Plan、Run/Result 契约测试通过；
- OAC 路由、Agent 切换/退出、Plan 确认、回调和历史读取 E2E 通过；
- 活动 Plan、在途 Agent 和待回调 Event 已排空或明确终止；
- Cutover Watermark、非敏感配置快照和回滚演练已保存。

该门禁不要求旧 Central 入口经历 7 天零流量，也不要求穷举未知直连调用方。中控退役后
没有兼容代理，且不再以 IRS 为回滚目标。7 天观察只适用于旧 Knowledge 入口。

### 9. Deadline 与 Circuit 属于 Provider Adapter 策略

OIR Provider 总 Deadline 默认 12 秒，`knowledge_sys` 内部检索预算默认 10 秒，均可通过
环境变量修改。OIR 请求链不自动重试。同一 Provider 在 30 秒内连续 5 次 timeout、
unavailable 或 5xx 后熔断 30 秒，再允许一次 half-open 探测。

empty、denied、JWT 4xx 和业务校验错误不计入熔断。熔断映射为 unavailable，再按
Knowledge Requirement 处理。Knowledge Admin 上传和重建使用独立长任务超时。

## Risks / Trade-offs

- [Provider 外部故障影响 required Agent] → 使用明确状态、12 秒 Deadline 和熔断，不自动重试。
- [正文通过运行记录形成新副本] → Invocation 前后实行持久化投影和正文扫描，异步载荷短 TTL 清除。
- [Memory 配置曾依赖 Knowledge fallback] → 先固化有效配置和行为基线，再删除回退。
- [不可逆清理误伤 Memory] → Manifest、Dry Run、未知结构 fail closed 和前后 Memory 不变性对账。
- [产品改名被误解为存储迁移] → 在配置、健康和测试中区分 Product Name 与 Storage Identifier。
- [Central 与 Knowledge 退役条件混淆] → 使用两个独立门禁；7 天观察仅约束旧 Knowledge 入口。

## Migration Plan

1. 重写本活动 OpenSpec，删除 OIR 拥有 Knowledge 的任务和规格。
2. 将可复用 Knowledge Contract Fixtures 与 Golden Dataset 迁入 `knowledge_sys`。
3. 固化 Memory 显式配置和不变性基线。
4. 在 OIR 建立 Provider-neutral Knowledge Provider、Requirement、Mode、Handle 和瞬时投影。
5. 在 `knowledge_sys` 建立正式身份、JWT/JWKS、Retrieval Trace、10 秒预算和长任务超时。
6. 将 OAC/Coze Knowledge 链路和 OIR Provider Adapter 切换到 `knowledge_sys`。
7. 删除 OIR Knowledge HTTP/Host Adapter/存储/索引并执行受控数据清理。
8. 删除飞书同步，完成 OIR Central 承接、排空、Watermark、快照和回滚演练。
9. 从 `knowledge_sys` 删除 IRS 中控模块，完成仓库、包、进程和部署正式改名。
10. 切换测试 `knowledge-sys-test`，通过跨系统、Provider、清理和 Memory 门禁。
11. 同轮发布生产 `knowledge-sys` 并退役 IRS 中控。
12. 旧 Knowledge 入口连续 7 天零流量后删除兼容层，完成最终验收。

## Superseded History

2026-07-23 以前完成的 OIR Knowledge Asset/Chunk/Admin、`oir_knowledge_vectors` 重建、
Knowledge Compat Facade 和相关 Shadow/Golden 验证仅证明当时实现，不再是当前目标。
这些记录不得作为保留 OIR Knowledge 能力的依据；可复用 fixture 和 golden 将迁往
`knowledge_sys`，其余只保留最小审计指针。
