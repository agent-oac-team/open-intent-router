# mem0 记忆闭环集成说明

本文说明 OIR 接入 mem0 OSS 记忆能力时的配置、运行边界、失败策略、调试方式和知识向量过渡约束。

## 目标边界

- OIR 继续拥有记忆治理事实：`memory_items` 保存 accepted memory，`memory_events` 保存 policy、mem0 add/search/delete history、外部 ID 映射和错误事件。
- mem0 作为策略层，负责 extraction、semantic search、merge/update 和 memory vector index。
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
DATABASE_URL=postgresql+asyncpg://oir:replace-with-local-password@127.0.0.1:5432/oir
MEMORY_STRATEGY_PROVIDER=mem0
MEMORY_MEM0_FAIL_CLOSED=false
MEMORY_MEM0_VECTOR_PROVIDER=milvus
MEMORY_MEM0_MILVUS_COLLECTION=oir_memory_vectors
MEMORY_MEM0_MILVUS_URI=.data/oir_memory_milvus.db
MEMORY_MEM0_HISTORY_BACKEND=postgresql
MEMORY_MEM0_HISTORY_DATABASE_URL=postgresql+asyncpg://oir:replace-with-local-password@127.0.0.1:5432/oir
KNOWLEDGE_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
KNOWLEDGE_EMBEDDING_API_KEY=replace-with-real-key
KNOWLEDGE_EMBEDDING_MODEL=text-embedding-v4
KNOWLEDGE_EMBEDDING_DIM=1024
```

也可以使用短字段配置 embedding；优先级为 `MEMORY_MEM0_*`、`EMBEDDING_*`、`KNOWLEDGE_EMBEDDING_*`：

```env
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=replace-with-real-key
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=1024
```

mem0 LLM 未显式配置 `MEMORY_MEM0_LLM_*` 时，会复用当前 router 的 OpenAI-compatible LLM 配置；本地已验证复用 DeepSeek：

```env
ROUTER_LLM_PROVIDER=openai_compatible
ROUTER_LLM_MODEL=deepseek-chat
ROUTER_LLM_BASE_URL=replace-with-current-deepseek-base-url
ROUTER_LLM_API_KEY=replace-with-real-key
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
DATABASE_URL=postgresql+asyncpg://oir:replace-with-password@localhost:5432/oir
```

`MEM0_CONFIG_JSON` 是高级覆盖项，会覆盖显式字段生成的 mem0 config。不要在仓库中提交真实 key、token 或数据库密码。

## 失败策略

- local fallback：`APP_ENV=local` 且 `MEMORY_MEM0_FAIL_CLOSED=false` 时，mem0 初始化或 add/search/delete 失败会降级到 OIR repository，并在 `memory_events` 与 debug metadata 中标记 `degraded` 和错误摘要。
- fail-closed：`MEMORY_MEM0_FAIL_CLOSED=true` 或未显式设置且 `APP_ENV!=local` 时，mem0 失败会返回结构化 error 或 rejected write decision，不报告为 mem0 成功。
- PostgreSQL history：mem0 SDK 当前文档仍显示 `history_db_path` 为 SQLite 内部 history。OIR 不把该 SQLite 文件视作 canonical，canonical ledger 写入 PostgreSQL `memory_events`。
- Milvus Lite 兼容：OIR 在 mem0 client 初始化后会显式加载 `oir_memory_vectors` collection，并把 mem0 Milvus search 的 `output_fields=["*"]` 转成 `["id", "metadata"]`，避免 Milvus Lite 已 release collection 或 search 命中但 metadata 为空。

## 调试入口

- `GET /api/v1/runtime/config`：查看 memory provider、mem0 collection、Milvus Lite URI、history backend、fail-closed 和静态健康状态。
- `GET /api/v1/memories/debug`：查看当前记忆项、事件、mem0 degraded 状态、最近错误和 `memory_id` 到 `mem0_memory_id` 的映射。
- 响应不会暴露 API key、Milvus token 或数据库密码，只显示 `*_configured` 类布尔状态或非敏感 collection/URI。

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
    CP --> INV["InvocationService / Agent Invoker"]
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

期望验证项：

- mem0 client 可由 `Memory.from_config()` 初始化。
- Milvus Lite 文件 URI 可用，collection 为 `oir_memory_vectors`。
- 阿里 OpenAI-compatible embedding 配置可用，模型为 `text-embedding-v4`，维度为 1024。
- 写入候选经过 OIR policy 后进入 mem0，并在 `memory_items` 与 `memory_events` 中保存 OIR ID、mem0 ID、collection、embedding model、status。
- 后续 recall 能把 mem0 search 结果映射到既有 `memory_context`。

2026-07-09 本地真实 smoke 记录：

- 配置：真实 PostgreSQL `oir` role + `oir` database、mem0 SDK、Milvus Lite `.data/oir_memory_milvus.db`、`oir_memory_vectors`、DashScope/OpenAI-compatible `text-embedding-v4`、1024 维、当前 DeepSeek router LLM。
- 结果：`SMOKE_OK`，mem0 写入、PostgreSQL/OIR ledger、Milvus Lite collection、`memory_context` 召回闭环通过，`events=6`。
- 非阻塞提示：未安装 `mem0ai[nlp]` 时 spaCy lemma/full model 会提示缺失，当前闭环仍通过；如后续需要更强 BM25/实体处理，可再安装 NLP extra 并单独回归。

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
