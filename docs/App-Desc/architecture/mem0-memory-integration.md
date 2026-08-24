# mem0 记忆闭环集成说明

本文说明 OIR 接入 mem0 OSS 记忆能力时的配置、运行边界、失败策略、调试方式和知识向量过渡约束。

## 目标边界

- PostgreSQL 是唯一 canonical store：`memory_items` 保存 current projection，`memory_revisions` 保存版本链，`memory_events` 保存无敏感正文审计，formation job/outbox 保存可恢复工作状态。
- OIR 的 formation model/projector 只提出候选，确定性 policy 和 lifecycle service 负责 evidence、DLP、ADD/UPDATE/DELETE/NOOP/PENDING/REJECT、revision 与硬删除。
- mem0/Milvus 只负责已治理 memory 的派生向量写入、原位更新、删除和 semantic search；索引可从 PostgreSQL active projection 完整重建。
- 普通自然语言先由 formation model 输出严格 semantic contract；hard rules 和 structured semantic validator 独立通过后才可进入 lifecycle。临时语言正则和 verifier 都不能直接授权 provider side effect。
- 下游 Agent 只接收稳定的 `memory_context`，不直接感知 mem0、Milvus 或 PostgreSQL ledger。

## Collection 职责

| Collection | 用途 | 当前状态 | 迁移要求 |
| --- | --- | --- | --- |
| `oir_memory_vectors` | mem0/OIR 记忆向量 | 本 change 接入 | 不得与知识 collection 混用 |
| `oac_knowledge_chunks` | IRS 现有知识向量 | 过渡期保留 | 检索结果必须保留 collection provenance |
| `oir_knowledge_vectors` | OIR 后续知识向量 | 后续迁移目标 | 基于 canonical chunks reindex |

Milvus collection 是派生索引，不是 canonical storage。知识资产、chunk、引用和权限元数据必须以 PostgreSQL canonical metadata 为准。若 embedding model、dimension、chunking strategy、vector schema 或 filter semantics 不一致，必须从 canonical chunks 重新索引，不能复制旧向量冒充迁移。

建议后续知识迁移 manifest 至少包含：

| 字段 | 说明 |
| --- | --- |
| `old_asset_id` / `old_chunk_id` / `old_index_id` | IRS 侧资产、chunk、索引身份 |
| `new_asset_id` / `new_chunk_id` / `new_index_id` | OIR 侧资产、chunk、索引身份 |
| `old_collection` / `new_collection` | 例如 `oac_knowledge_chunks` 到 `oir_knowledge_vectors` |
| `embedding_model` / `embedding_dimension` | reindex 判定依据 |
| `chunking_strategy` / `vector_schema_version` | chunk 与向量 schema 版本 |
| `migration_status` | `pending`、`reindexed`、`skipped`、`retired`、`failed` |

## 推荐配置

默认 repository-only：

```env
MEMORY_STRATEGY_PROVIDER=memory
```

本地真实 mem0 闭环：

```env
MEMORY_DATABASE_URL=postgresql+asyncpg://oir:replace-with-local-password@127.0.0.1:5432/oir
MEMORY_STRATEGY_PROVIDER=mem0
MEMORY_MEM0_FAIL_CLOSED=false
MEMORY_MEM0_VECTOR_PROVIDER=milvus
MEMORY_MILVUS_COLLECTION=oir_memory_vectors
MEMORY_MILVUS_URI=.data/oir_memory_milvus.db
MEMORY_MILVUS_TOKEN=
MEMORY_MILVUS_DB_NAME=
MEMORY_MEM0_HISTORY_BACKEND=postgresql
MEMORY_MEM0_HISTORY_DATABASE_URL=postgresql+asyncpg://oir:replace-with-local-password@127.0.0.1:5432/oir
MEMORY_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MEMORY_EMBEDDING_API_KEY=replace-with-real-key
MEMORY_EMBEDDING_MODEL=text-embedding-v4
MEMORY_EMBEDDING_DIMS=1024
```

Memory 不再读取 `DATABASE_URL`、通用 `EMBEDDING_*`、`KNOWLEDGE_*` 或重复的
`MEMORY_MEM0_MILVUS_*` / `MEMORY_MEM0_EMBEDDING_*`。`MEMORY_MODE=observe|on`
时必须显式提供 `MEMORY_DATABASE_URL`；选择 mem0 策略时还必须提供
`MEMORY_MILVUS_URI`、`MEMORY_MILVUS_COLLECTION`、`MEMORY_EMBEDDING_MODEL`
和 `MEMORY_EMBEDDING_DIMS`。缺项会在 Settings/启动阶段列出并失败，不会创建默认
Collection。

mem0 只有在显式配置 `MEMORY_MEM0_LLM_*` 时才获得 LLM 配置，不复用 Router LLM：

```env
MEMORY_MEM0_LLM_MODEL=qwen-plus
MEMORY_MEM0_LLM_BASE_URL=https://provider.example/v1
MEMORY_MEM0_LLM_API_KEY=replace-with-real-key
```

PostgreSQL 初始化：

```bash
psql postgresql://<postgres-admin>@127.0.0.1:5432/postgres -f sql/postgresql_schema.sql
```

`sql/postgresql_schema.sql` 默认创建 `oir` role/database 并在 `oir` 专属库里建表。不要把 OIR 表继续建在 IRS/OAC 的 `oac` database 中，否则 memory/history 与旧系统数据会混在一起，后续迁移审计会变困难。

生产或验收建议：

```env
APP_ENV=production
MEMORY_STRATEGY_PROVIDER=mem0
MEMORY_MEM0_FAIL_CLOSED=true
MEMORY_DATABASE_URL=postgresql+asyncpg://oir:replace-with-password@localhost:5432/oir
```

## 失败策略

- local fallback：`APP_ENV=local` 且 `MEMORY_MEM0_FAIL_CLOSED=false` 时，mem0 初始化或 add/search/delete 失败会降级到 OIR repository，并在 `memory_events` 与 debug metadata 中标记 `degraded` 和错误摘要。
- fail-closed：`MEMORY_MEM0_FAIL_CLOSED=true` 或未显式设置且 `APP_ENV!=local` 时，mem0 失败会返回结构化 error 或 rejected write decision，不报告为 mem0 成功。
- PostgreSQL history：mem0 SDK 当前文档仍显示 `history_db_path` 为 SQLite 内部 history。OIR 不把该 SQLite 文件视作 canonical，canonical ledger 写入 PostgreSQL `memory_events`。
- Milvus Lite 兼容：OIR 在 mem0 client 初始化后会显式加载 `oir_memory_vectors` collection，并把 mem0 Milvus search 的 `output_fields=["*"]` 转成 `["id", "metadata"]`，避免 Milvus Lite 已 release collection 或 search 命中但 metadata 为空。

## 调试入口

- `GET /api/v1/runtime/config`：查看 memory provider、有效 SQL/Milvus/embedding
  配置（敏感值已脱敏）、显式配置来源、history backend、fail-closed 和静态健康状态。
- `GET /api/v1/memories/debug`：查看当前记忆项、事件、mem0 degraded 状态、最近错误和 `memory_id` 到 `mem0_memory_id` 的映射。
- 响应不会暴露 API key、Milvus token 或数据库密码，只显示 `*_configured` 类布尔状态或非敏感 collection/URI。

可用公开 API 生成无正文、可比较的配置与行为快照：

```bash
.venv/bin/python scripts/capture_memory_invariance.py \
  --base-url http://127.0.0.1:8000 \
  --tenant-id <isolated-tenant> \
  --user-id <isolated-user> \
  --recall-query "<representative-query>" \
  --exercise-crud \
  --output <environment-specific-output.json>
```

工具默认只读；`--exercise-crud` 会创建、召回并删除一个唯一探针。报告只保留哈希、
ID、计数、状态和安全配置，不保存 Memory 正文。测试/生产环境应分别采集变更前后报告，
本地报告不能替代环境验收。

自动形成的运行配置和上线步骤见 [`conversation-memory-formation-rollout.md`](../../App-Adr/develop/skills/runbooks/conversation-memory-formation-rollout.md)。`/runtime/config` 还会暴露非敏感的 formation mode/version、worker 开关、queue depth、oldest pending、dead-letter、index out-of-sync 和 deletion pending 状态；完整指标由管理员接口 `GET /api/v1/admin/memories/metrics` 提供。

## 端到端流程

```mermaid
flowchart TD
    U["用户输入"] --> H["Host App / 测试台"]
    H --> API["POST /api/v1/route 或 /route-and-invoke"]
    API --> R["RouterService: 候选 Agent 过滤与路由"]
    R --> A["AgentContextAssemblyService"]

    A --> MR["MemoryService.recall"]
    MR --> MA["Mem0MemoryAdapter.search"]
    MA --> M0S["mem0 Memory.search"]
    M0S --> EMBQ["阿里 embedding: text-embedding-v4 / 1024"]
    EMBQ --> MLITE["Milvus Lite: oir_memory_vectors"]
    M0S --> PGH1["PostgreSQL mem0 history / ledger"]
    MLITE --> MA
    PGH1 --> MA
    MA --> MC["memory_context: summary + items + status"]

    A --> CP["Context Pack 预算与裁剪"]
    MC --> CP
    CP --> INV["InvocationService / v2 Runtime Adapter"]
    INV --> AG["目标 Agent / Tool / Workflow"]
    AG --> OUT["Agent 结果 / Plan 结果 / 对话回复"]
    OUT --> RESP["RouteResponse / InvocationResult 返回给 Host"]

    OUT --> CAND["记忆候选提取: message/result/plan"]
    CAND --> WG["MemoryService.write_candidates"]
    WG --> POL["OIR 记忆治理: scope / tenant / sensitivity / confidence / TTL"]
    POL -->|拒绝| REJ["MemoryWriteDecision: rejected + reason"]
    POL -->|接受| ADD["Mem0MemoryAdapter.add"]
    ADD --> M0A["mem0 Memory.add"]
    M0A --> EMBA["阿里 embedding: text-embedding-v4 / 1024"]
    EMBA --> MLITE
    M0A --> PGH2["PostgreSQL mem0 history / external ID mapping"]
    ADD --> OIRPG["OIR PostgreSQL: memory_items / memory_events"]
    OIRPG --> NEXT["后续请求可召回"]
    NEXT --> MR

    subgraph KnowledgeTransition["知识向量过渡边界"]
        OKC["IRS: oac_knowledge_chunks"]
        OIRK["OIR: oir_knowledge_vectors"]
        KNOTE["双 collection 并行，保留 provenance；迁移必须基于 canonical chunks reindex"]
        OKC --> KNOTE
        OIRK --> KNOTE
    end
```

## 可选真实基础设施 Smoke

默认单元测试不依赖真实 mem0、PostgreSQL、Milvus Lite 或阿里 embedding。真实验收可在配置好 `.env` 后运行：

```bash
pip install -e ".[memory,test]"
MEMORY_STRATEGY_PROVIDER=mem0 \
MEMORY_MEM0_FAIL_CLOSED=true \
.venv/bin/python scripts/smoke_mem0_memory_loop.py
```

如需同时调用真实 Formation Provider 验证严格 JSON、受控 retry/dead-letter 和
“访前准备 + 我喜欢吃猪肉”形成/召回闭环：

```bash
OIR_SMOKE_REAL_FORMATION=1 \
MEMORY_STRATEGY_PROVIDER=mem0 \
MEMORY_MEM0_FAIL_CLOSED=true \
.venv/bin/python scripts/smoke_mem0_memory_loop.py
```

该模式额外要求 `ROUTER_LLM_BASE_URL`、`ROUTER_LLM_API_KEY` 和可用的
`MEMORY_FORMATION_MODEL`。Provider 首次返回无效严格 JSON 时由同一 Formation Job 受控重试；
重试耗尽、未形成猪肉偏好、index 未 ready 或 recall 未命中都会使 smoke 非零退出。

期望验证项：

- mem0 client 可由 `Memory.from_config()` 初始化。
- Milvus Lite 文件 URI 可用，collection 为 `oir_memory_vectors`。
- 阿里 OpenAI-compatible embedding 配置可用，模型为 `text-embedding-v4`，维度为 1024。
- preference 经 policy 形成 revision 1，真实 recall 命中后以同一 OIR ID 和 mem0 external ID 原位更新为 revision 2。
- 用户删除后 current/revision 正文和 provider vector 均被硬删除，只保留无正文 tombstone。
- `infer=False` ADD 只形成一个 provider vector；rebuild 只从 PostgreSQL active projection 恢复索引。
- owned 未完成 Plan 可由“继续上次任务”定位，同用户无关新任务不会被旧 task memory 强制续接。
- 可选真实 Formation 模式必须生成有证据的 food preference，并在 canonical Revision 与 Milvus index 都 ready 后由后续 recall 命中。

2026-07-09 本地真实 smoke 记录：

- 配置：真实 PostgreSQL `oir` role + `oir` database、mem0 SDK、Milvus Lite `.data/oir_memory_milvus.db`、`oir_memory_vectors`、DashScope/OpenAI-compatible `text-embedding-v4`、1024 维、当前 DeepSeek router LLM。
- 结果：`SMOKE_OK`，mem0 写入、PostgreSQL/OIR ledger、Milvus Lite collection、`memory_context` 召回闭环通过，`events=6`。
- 非阻塞提示：未安装 `mem0ai[nlp]` 时 spaCy lemma/full model 会提示缺失，当前闭环仍通过；如后续需要更强 BM25/实体处理，可再安装 NLP extra 并单独回归。

2026-07-14 扩展闭环 smoke 记录：

- 配置：真实 PostgreSQL、mem0ai `2.0.11`、Milvus Lite `oir_memory_vectors`、真实 OpenAI-compatible embedding；使用随机隔离 tenant/user/Plan，脚本结束时清理 active smoke 数据。
- 结果：`SMOKE_OK`；preference ADD/recall/revision UPDATE/delete、stable external-ID update、`infer=False` 单 vector、canonical rebuild、owned Plan continuation 和无关新任务隔离全部通过。
- 非阻塞噪音：PostHog 提示、gRPC fork warning、未安装 `mem0ai[nlp]` 的 spaCy 提示不影响闭环结果。

### Knowledge 侧 Milvus 真实向量检索 Smoke

knowledge 侧使用同一套本地基础设施约束：PostgreSQL 复用本机服务但写入 OIR 专属 `oir` database，Milvus 本地统一使用 Milvus Lite，embedding 沿用阿里 DashScope/OpenAI-compatible 配置。`.env` 推荐配置：

```env
DATABASE_URL=postgresql+asyncpg://oir:replace-with-local-password@127.0.0.1:5432/oir
STORAGE_BACKEND=database
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=replace-with-real-key
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=1024
KNOWLEDGE_ENABLED=true
KNOWLEDGE_VECTOR_BACKEND=milvus
KNOWLEDGE_MILVUS_COLLECTION=oir_knowledge_vectors
KNOWLEDGE_MILVUS_URI=.data/oir_knowledge_milvus.db
```

运行命令：

```bash
.venv/bin/python scripts/smoke_knowledge_milvus_vector_search.py
```

脚本验证项：

- 在 PostgreSQL `knowledge_sources` / `knowledge_chunks` 写入 canonical source/chunk。
- 对两个 knowledge chunk 调用阿里 embedding，并写入 Milvus Lite `oir_knowledge_vectors`。
- 通过 `KnowledgeService.search()` 走 `MilvusKnowledgeVectorStore`，不是 repository keyword fallback。
- Milvus search 返回 `chunk_id` 后，再从 PostgreSQL canonical chunk 回填正文、title、URI 和 citation。
- 在 `knowledge_retrieval_logs` 写入检索日志。

2026-07-09 本地真实 smoke 记录：

- 配置：真实 PostgreSQL `oir` role + `oir` database、Milvus Lite `.data/oir_knowledge_milvus.db`、`oir_knowledge_vectors`、DashScope/OpenAI-compatible `text-embedding-v4`、1024 维。
- 结果：`SMOKE_OK`，knowledge Milvus Lite 真实向量检索闭环通过，写入 2 个 chunk，首位命中目标 chunk，retrieval log 写入 1 条。
- 迁移边界：本次验收不写 `oac_knowledge_chunks`。`oac_knowledge_chunks` 仍作为 IRS 现有知识 collection 过渡保留；OIR 新索引写入 `oir_knowledge_vectors`，未来迁移必须从 PostgreSQL canonical chunks reindex。
