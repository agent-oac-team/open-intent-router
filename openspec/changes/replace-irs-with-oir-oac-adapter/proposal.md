## Why

OIR 已经承接 IRS 的中控职责，但当前迁移计划仍要求 OIR 建设和代理完整 Knowledge 能力，
这会让路由编排核心同时拥有知识资产、索引、管理和审计生命周期。现有知识事实已经由旧
IRS 的 Knowledge 模块稳定承载，并被 OAC、Coze 和其他下游直接使用；OIR 的 Knowledge
接口没有已登记调用方，两套知识副本也不应继续形成双主。

本变更必须成为唯一迁移计划，明确两个可独立部署的事实源：OIR 独占中控运行事实，
`knowledge_sys` 独占组织知识事实。IRS 仅指代待退役的历史中控能力，不得成为 OIR Core
的运行依赖。

## What Changes

- 保留并完成 OIR 对 Central Route、Agent Registry、Session、Event、Plan、Run/Result、
  Canonical Turn、Context 和 Memory 的承接，满足 Central Retirement Gate 后退役 IRS 中控。
- 将原 IRS 仓库、包、进程和部署正式更名为 `knowledge_sys` / `knowledge-sys`，使其只保留
  Knowledge Search、Grouped Search、Exact Read、Assets、Chunks、Admin、ACL、索引、
  缓存和 Retrieval Trace；既有 SQL/Milvus 存储标识与 PostgreSQL 角色 `oac` 不改名。
- 删除 OIR 的 Knowledge HTTP、管理、存储、索引、迁移、兼容代理和跨请求 Knowledge
  Cache；OIR 仅保留 Provider-neutral 的 `KnowledgeProvider.retrieve` 及既有
  `AgentDefinition.context.knowledge` / Invocation `knowledge_context`。
- 保持 Evidence Provider 与 Knowledge Provider 独立：前者只为 Route Decision 提供证据，
  后者只为 Agent Context 检索知识，不能返回 Route Override、候选 Agent 或目标 Agent。
- OAC 浏览器与 Coze 通过 OAC Backend 使用短时 JWT 访问 `knowledge_sys`；OIR 的
  Knowledge Provider Adapter 也使用 JWT/JWKS 直连，但 OIR Core 不依赖 `knowledge_sys`
  或 IRS 专有类型、URL、接口和存储。
- 不迁移或合并 OIR 的知识副本。生成无正文 Manifest 后清理 OIR Knowledge 表、Collection、
  本地向量和持久化正文；Memory Store、Collection、向量和数据保持不变。
- 飞书 Agent Registry Sync 直接删除；其他 IRS 中控模块在承接测试、运行态排空、
  Cutover Watermark、非敏感快照和回滚演练通过后删除。
- 测试环境先切换至 `knowledge-sys-test`，门禁通过后同轮发布生产 `knowledge-sys`；
  IRS 中控退役后不回滚。旧 Knowledge `/central-api` 入口只在边缘保留 7 天兼容期。

## Capabilities

### New Capabilities

- `canonical-conversation-turn`: OIR 对可信语义轮次、Result 关联和 Memory Formation 触发的事实边界。
- `delegated-external-run`: OIR 对宿主执行 Agent 的 Run、Ticket、回调和终态收口边界。
- `migration-shadow-cutover`: 双事实源切换、Central Retirement Gate、数据清理、环境切换和旧入口退役门禁。

### Modified Capabilities

- `oac-irs-compat-adapter`: 只承接历史 Central/Registry 契约到 OIR 的宿主适配，不再拥有或代理 Knowledge。
- `agent-registry`: OIR Registry 成为唯一中控主写源，飞书和 IRS Registry 退出。
- `knowledge-context-retrieval`: OIR 通过 Provider-neutral Knowledge Provider 获取瞬时 Agent Context，不拥有 Knowledge 数据或 HTTP API。
- `agent-context-contract`: 保留通用 Knowledge 声明与 `knowledge_context`，增加 Requirement、Mode 和受控 Handle 规则。
- `knowledge-vector-transition`: 从“双 Knowledge Collection 迁移”改为删除 OIR Knowledge 副本并证明 Memory 向量数据不变。
- `memory-context-governance`: Memory 只使用显式 `MEMORY_*` 配置，外部 Knowledge Context 不参与 Memory Formation。

## Removed Capabilities

- `knowledge-asset-management`: OIR 不再建设 Knowledge Asset、Chunk、Search、Read、Admin、入库或索引能力；这些能力由 `knowledge_sys` 独占。

## Impact

- OIR：继续拥有 Router、Registry、Turn、Run/Result、Plan/Event、Context、Memory 和通用
  Provider 端口；删除 Knowledge HTTP、存储、索引、管理及 OAC Knowledge Adapter。
- `knowledge_sys`：成为 Knowledge 唯一 Canonical Data 与 API 所有者，并删除历史中控模块。
- OAC：Central Base URL 指向 OIR；Knowledge Base URL 独立指向 `knowledge_sys`，浏览器
  和 Coze 仍由 OAC Backend 代理。
- 数据：不迁移 Knowledge 数据，不修改既有 SQL/Milvus 名称；仅清理 OIR Knowledge 副本，
  并用前后对账证明 Memory 不变。
- 历史：此前“由 OIR 重建和接管 Knowledge”的已完成验证仅作为 `superseded` 审计事实，
  不再代表当前架构或待办。
