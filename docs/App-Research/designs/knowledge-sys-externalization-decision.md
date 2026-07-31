# knowledge_sys 与 OIR 职责分离决策

> 状态：accepted
> 日期：2026-07-30

组织知识能力独立为正式产品 `knowledge_sys`，本轮同步重命名原
`intent_recon_sys` 仓库与部署；IRS 只表示迁移验证完成前待退役的历史中控能力。
OIR 不拥有知识资产、索引或知识管理生命周期，只通过通用
Evidence / Knowledge Provider interface 消费外部证据，OIR Core 不得依赖 IRS
专有契约或实现。IRS 中控能力在 OIR 迁移回归通过后退役。

正式标识统一为：GitHub 仓库和本地目录 `knowledge_sys`、Python distribution
`knowledge-sys`、生产服务与进程 `knowledge-sys`、测试服务与进程
`knowledge-sys-test`，部署目录分别为 `/var/www/knowledge-sys` 和
`/var/www/knowledge-sys-test`。`intent_recon_sys`、IRS 和 `oac-central*` 不再作为
新系统的正式标识。

产品改名不触发数据层重命名：既有 SQL 数据库名、Milvus 数据库与 Collection 名保持
不变，不执行对应迁移；knowledge_sys 的 PostgreSQL 数据库角色继续使用 `oac`。
默认配置中现有 SQL 数据库为 `oac`、Milvus Collection 为
`oac_knowledge_chunks`，这些 Storage Identifier 不再被解释为产品名称。

knowledge_sys 最终只保留 Knowledge Search、Grouped Search、Exact Read、Assets、
Chunks、Knowledge Admin、文件处理与索引、ACL、Retrieval Trace、审计读取、健康检查
和必要运维指标。Central Route、Agents、Sessions/History、Events、Plans、Results、
LLM Route Logs、Agent Registry 及其 API、Service、Repository、Schema 和配置全部
退出。飞书 Agent Registry Sync 及相关配置无需 OIR 提供替代，直接删除；其余中控
接口与模块必须逐项验证 OIR 已承接后才可删除。IRS migration write-freeze 和 DeepSeek
路由等中控专属依赖随对应能力删除。

除飞书同步外，中控退役必须先通过 OIR 的 Central、Registry、Agent、Session、Event、
Plan、Run/Result 契约测试，完成 OAC 路由、Agent 切换/退出、Plan 确认、回调与历史
读取 E2E，并排空或明确终止 IRS 活动 Plan、在途 Agent 和待回调 Event；同时保存
Cutover Watermark、非敏感配置快照并完成回滚演练。门禁通过后，同一退役 Release
删除中控代码与对应数据库表，只保留数量、状态和聚合 Hash，不迁移历史正文或运行态。
旧 IRS 中控入口无需连续 7 天零流量，也不要求先穷举未知直连调用方；这一例外不影响
旧 Knowledge `/central-api` 入口已确定的 7 天兼容期。

knowledge_sys 现有知识存储是唯一 Canonical Data。OIR 中来自相同源文件的知识副本
不迁移、不合并；删除前只保存只读 Manifest、数量和 Hash 对账证据。OIR 与
knowledge_sys 当前分别存在 269 和 270 个 Chunk，这一解析差异不通过数据复制抹平，
由 knowledge_sys 的 Golden Dataset 和现有调用链验证其权威结果。

Knowledge HTTP 接口由 knowledge_sys 独占。OIR 删除 Native 与 OAC Host Adapter
中的 Knowledge Search、Grouped Search、Read、Assets、Chunks 和 Admin 接口，不保留
代理、兼容门面或 IRS fallback；OAC 与其他下游直接调用 knowledge_sys。OIR 只在内部
Context 组装过程中通过通用 Provider interface 消费所需证据。

knowledge_sys 原样保留既有 `/api/v1/knowledge/**` 与
`/api/v1/admin/knowledge/**` 路径和响应契约，正式网关路径为 `/knowledge-api`，
域名由环境配置。OAC 的 `KNOWLEDGE_API_BASE_URL` 与
`COZE_KNOWLEDGE_BASE_URL` 必须显式指向 knowledge_sys，不得回退
`CENTRAL_API_BASE_URL`。旧 `/central-api` 可在迁移期直接将 Knowledge 路径转发到
knowledge_sys，但不得经过 OIR；全部登记调用方切换且旧入口连续 7 天零流量后删除
兼容转发。knowledge_sys 不保留任何中控路由接口。

OAC 负责认证终端用户并向 knowledge_sys 传递签名后的 Knowledge Principal；
knowledge_sys 独立认证调用服务，并最终执行资产 ACL、用途与读写授权。OIR 只能通过
Knowledge Provider Adapter 传递已验证、重新签名的主体，不得把客户端自报的
`user_tags` 当作授权事实。Coze 通过 OAC 使用只读服务主体，不能冒充终端用户；管理
操作必须同时携带明确操作者与 Admin Scope。knowledge_sys 不依赖
`X-OIR-HOST-*`、OIR 专有代码或未签名的 `X-OAC-*` 身份头。

正式鉴权契约为 HTTPS 上的短时 Bearer JWT。OAC 与 OIR 各自持有签发私钥，
knowledge_sys 通过受信 Issuer 的 JWKS 公钥独立验签；JWT 必须包含 `iss`、`sub`、
`aud=knowledge_sys`、`iat`、`exp`、`jti`、`tenant_id`、`principal_type` 和
`scopes`，最长有效期 5 分钟，并通过 `kid` 支持轮换。最小 Scope 为
`knowledge:read` 与 `knowledge:admin`。用户版本或权限必须来自签名 Claims，
请求体中的 `user_id/user_tags` 不构成授权事实。旧 `ADMIN_SYNC_TOKEN` 与 OIR HMAC
仅是迁移凭据，不属于正式契约。

正式 `/knowledge-api` 自启用起只接受 JWT。迁移期旧 `/central-api` 边缘兼容层负责
验证旧凭据、换发短时 JWT 后再调用 knowledge_sys；旧 `ADMIN_SYNC_TOKEN` 只由该
兼容层验证。既有匿名读取调用方必须先登记为只读服务主体，未签名身份字段不得被提升为
受信 Claims。旧入口连续 7 天零流量后，兼容路由与凭据转换逻辑同时删除；
knowledge_sys Core 不实现双鉴权。

OAC 浏览器继续调用 OAC Go Knowledge Proxy；OAC 验证登录用户后签发短时 Knowledge
JWT 并直接调用 knowledge_sys，浏览器不持有服务私钥，knowledge_sys 也不依赖 OAC
Session。Coze 继续通过 OAC 只读代理换取只读服务 JWT；已登记后端可用自身受信 Issuer
直连。OIR 仅通过 Knowledge Provider Adapter 直连 knowledge_sys，不提供 Knowledge
HTTP Proxy。

OIR 保留两个独立 interface：Evidence Provider 只服务 Route Decision，可提供意图提示
和受控固定命中；Knowledge Provider 只服务 Context 检索，返回内容、引用、分数、状态
和 trace。knowledge_sys Adapter 只实现 Knowledge Provider，外部知识结果不得携带
`route_override`、目标 Agent 或其他路由控制语义。未配置 Knowledge Provider 时，OIR
仍能独立完成路由和编排。

Knowledge Provider 的实现选择归 OIR 部署与租户策略层。Agent Definition 不包含
`provider_id`、URL 或供应商名称；每次调用由 Composition Root 解析一个 Provider，
未解析到实现时执行 Knowledge Requirement 语义。未来多后端查询由接口后的 Composite
Provider 实现，不改变 Agent 契约。knowledge_sys 只是默认 HTTP Adapter 的实现之一。

OIR Core 的 Knowledge Provider 只暴露异步 `retrieve(request) -> result`。请求字段
限定为 request/trace 标识、已验证 Principal、query、purpose、consumer、逻辑
`source_ids/source_tags` 以及 `max_items/max_chars/deadline_ms` 预算；结果字段限定为
`status`、带 Citation 的 items、trace、warnings、稳定 error code 与 truncated。
Provider 状态为 `ok | empty | denied | timeout | unavailable | error`。Knowledge
Requirement 由 OIR Context 组装层处理，Provider 不返回 Summary、Route Decision、
目标 Agent 或管理能力。knowledge_sys Adapter 负责映射既有 Knowledge HTTP API。

保留 Agent Definition 的 `source_ids/source_tags` 字段，但 `source_ids` 只表示长期
稳定的逻辑 Source Key，不得承载数据库主键、Collection 名或文件路径；`source_tags`
只用于内容分类，不能作为授权凭据。Source Key 到 Asset、Group 和索引的解析全部由
knowledge_sys 完成，OIR 不验证来源是否存在。Citation 的最小必需字段只有逻辑
`source_id`；审计定位依赖结果级 `trace_id` 与条目级 `item_id`。`locator`、
`source_version` 和 `content_hash` 均不是 OIR Core 契约的必需字段。

跨请求 Knowledge Cache 由 knowledge_sys 独占，缓存键必须覆盖租户、主体权限版本、
查询、逻辑范围和索引版本，并由知识生命周期负责失效。OIR 不缓存或持久化知识结果，
只允许单次请求内调用去重；OIR 可维护不含知识正文的 Provider 健康状态与 Circuit
Breaker。该限制不适用于 OIR 私有的 Memory 缓存与召回。

Knowledge Context 正文只存在于本次 Agent 调用的内存载荷；异步交付确有需要时，仅可
进入加密、短 TTL 的临时载荷存储，并在任务终态后清除。OIR 的 Run、Event、Trace 和
日志只持久化 Citation、Provider Trace ID、Item ID、数量、状态和错误码；
`AgentRun.input_text` 落库前必须移除正文。长期检索日志与可复现快照归 knowledge_sys
所有，Knowledge Context 不得自动成为 OIR Memory Formation 的证据。

knowledge_sys 必须保存 Retrieval Trace 及当次返回的 Item ID 集合，并在可配置审计
保留期内保证 `trace_id + item_id` 可解析；未配置统一策略时默认保留 30 天。审计读取
必须重新鉴权，知识删除或权限收紧后可返回脱敏 Tombstone；保留期结束后返回明确的
`trace_expired`，不得静默解析为其他内容。OIR 不保存对应知识快照。

OIR 知识副本通过一次性、可审计迁移清理。清理前只导出表名、数量、Schema 版本和聚合
Hash Manifest，不备份正文；随后删除 Knowledge Sources、Chunks、Retrieval Logs、
Asset Groups、Assets、Asset Chunks、Import Jobs、Migration Manifests、Operation
Traces 九类表及对应 Repository/Migration，并删除 `oir_knowledge_vectors` 与 OIR
本地知识向量文件。既有 Run/Event/Trace JSON 只定向清除 `knowledge_context` 正文，
保留 trace、item、Citation、状态与数量；Agent Definition 的 Provider-neutral
`context.knowledge` 保留。Memory 表、Collection 与数据不得变更。清理工具必须先
Dry Run，遇到未知 JSON 结构立即停止。

现有 active `replace-irs-with-oir-oac-adapter` 是本轮唯一迁移计划，不创建并行计划。
其中所有要求 OIR 存储、检索、代理或管理 Knowledge 的未完成任务和当前态描述必须
删除，切流目标改为 OIR 独占中控事实、knowledge_sys 独占知识事实。OIR 中可复用的
Knowledge Contract Fixtures、Golden Dataset 与测试迁入 knowledge_sys；已完成验证
只保留标记 `superseded` 的最小审计记录并退出当前文档索引。非历史性的 OIR 语义检索
设计、待办和启用说明删除，OAC 文档不得继续描述 IRS-compatible OIR Knowledge
Adapter。

发布顺序为：先将 GitHub 仓库、本地目录与 Python 项目同步改为
`knowledge_sys/knowledge-sys` 并更新跨仓库引用，但不改存储标识；随后使用原有数据、
新 JWT 与独立 Knowledge URL 部署 `knowledge-sys-test`，完成 OIR 中控承接门禁、
OAC/Coze Knowledge E2E、Memory 不变性与 Knowledge Golden Tests。门禁通过后停止
`oac-central-test`、删除旧部署目录与中控模块，使测试环境只保留
`knowledge-sys-test`；同一轮再发布生产 `knowledge-sys` 并退役 IRS 中控，退役后
不再回滚到 IRS。旧 `/central-api` 仅保留 Knowledge 转发 7 天，中控接口不保留兼容
转发。

OIR 保留 Provider-neutral 的 `AgentDefinition.context.knowledge` 和 Agent Invocation
`knowledge_context`。前者只声明 Agent 的外部知识需求、范围和预算，后者只承载经 OIR
治理后的只读 Knowledge Context；两者不得暴露 knowledge_sys URL、存储、索引或知识
管理生命周期。Agent 不直接依赖 knowledge_sys，由 Knowledge Provider Adapter 统一
处理身份、超时、预算和引用。

`AgentDefinition.context.knowledge` 增加 Provider-neutral 的 Knowledge Requirement，
取值为 `optional | required`，默认 `optional`。`optional` 在 Provider 未配置、拒绝、
超时或出错时继续调用 Agent，并在结果中保留结构化状态与警告；`required` 遇到上述
情况时不得调用 Agent，而应返回结构化失败。Provider 成功响应但经授权、相关性和预算
治理后没有可用结果时，`required` 返回 `knowledge_not_found` 且不调用 Agent，
`optional` 则携带 `empty` 状态继续执行；Provider 故障使用独立的
`knowledge_unavailable` 状态。

Knowledge Requirement 与 Retrieval Mode 的组合规则为：`disabled` 只允许
`optional`；`prefetch` 支持 `optional|required` 并在调用前自动检索；
`controlled_retrieval + optional` 在未显式检索时仍可调用 Agent；
`controlled_retrieval + required` 必须在 Invocation 前提供 OIR 签发的短时 Knowledge
Context Handle。Handle 必须绑定租户、主体、Agent、Source Scope 与 Trace，缺失、
过期、失败或无结果时阻断；调用方自行构造的 `knowledge_context` 不可信。Agent 最终
仍接收现有 `knowledge_context`，Handle 只用于 OIR 内部治理和瞬时交付。

OIR 继续独立拥有 Memory 及其向量索引；使用向量数据库不等于拥有 Knowledge 能力。
Memory 配置必须全部使用显式 `MEMORY_*` 命名空间，不得回退到 `KNOWLEDGE_*`，并删除
`knowledge_transition_collections` 等过渡耦合元数据及相关校验。OIR Memory 与
knowledge_sys 可共享物理向量数据库集群，但必须使用独立数据库或 Collection、独立
权限且禁止跨服务读写。本轮不得删除、重建或迁移现有 Memory 数据。

Memory 配置解耦按同一轮两阶段执行：先将当前实际生效值显式写入
`MEMORY_EMBEDDING_*` 与 `MEMORY_MILVUS_*`，保持 Collection 名不变，并验证存量
数量、读取、召回、新增与删除；再移除全部 Knowledge 配置回退、过渡元数据和交叉
校验。Memory 启用但缺少显式配置时必须启动失败，不得静默切换配置或创建 Collection；
不重新 Embedding 或重建向量。

Knowledge Provider 的 OIR 总 Deadline 默认 12 秒，knowledge_sys 内部检索预算默认
10 秒，二者均由环境变量显式配置。OIR 请求链不自动重试；同一 Provider 在 30 秒内
连续 5 次 timeout、unavailable 或 5xx 后熔断 30 秒，再允许一次 Half-open 探测。
empty、denied、JWT 4xx 与业务校验错误不计入熔断。熔断映射为 unavailable 并继续
执行 Knowledge Requirement 语义；Admin 上传和重建使用独立超时。
