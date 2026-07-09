# add-agent-context-memory-knowledge 测试报告

版本：v1.2
原始测试日期：2026-07-08
修复复测日期：2026-07-09
测试负责人：Codex
适用变更：`openspec/changes/add-agent-context-memory-knowledge`
测试方案：`docs/agent-context-memory-knowledge-test-plan.md`

## 1. 修复复测结论

结论：通过。

2026-07-09 已完成缺陷修复和复测：

- `max_items=0` 在 memory/knowledge prefetch 链路不再回落默认值。
- `max_items=0` 在 controlled retrieval 链路不再触发知识检索。
- direct invoke 输入只带部分 context 时，会复用已有 context 并补齐缺失 context。
- `storage_backend=database` 下 memory/knowledge context store 已接入 database-backed repository，并补充 SQLite round-trip 测试。
- ruff lint 和 format check 已通过。

复测结果：

- `.venv/bin/python -m pytest`：86 passed，1 warning。
- `.venv/bin/python -m ruff check .`：通过。
- `.venv/bin/python -m ruff format --check .`：通过。
- `openspec validate add-agent-context-memory-knowledge --strict`：通过。
- `web/npm run test`：5 passed。
- `web/npm run build`：通过。

仍需单独环境验证：

- 50 并发 route/invoke 下 context 不串用户/租户/session 的压力验证。

2026-07-09 `integrate-mem0-memory-loop` 追加实现记录：

- 已新增 mem0 显式配置、Milvus Lite memory collection、PostgreSQL-backed OIR ledger、阿里 embedding 默认值、fail-closed/local fallback 分支和 debug/runtime 可观测字段。
- 已新增 fake mem0 单元/服务闭环测试，覆盖 add/search/delete、`mem0_memory_id` 映射、Milvus Lite collection load、Milvus Lite `id/metadata` 显式返回字段兼容、history event、policy-before-adapter、fail-closed、local degraded fallback 和敏感字段不泄漏。
- 已新增可选 smoke helper：`scripts/smoke_mem0_memory_loop.py`。真实 PostgreSQL、Milvus Lite、mem0 SDK 和阿里 embedding 凭证可用时，按 `docs/mem0-memory-integration.md` 运行并记录结果。
- 2026-07-09 真实 smoke 已通过：`DATABASE_URL=postgresql+asyncpg://oir:***@127.0.0.1:5432/oir`、`STORAGE_BACKEND=database`、`MEMORY_STRATEGY_PROVIDER=mem0`、`MEMORY_MEM0_FAIL_CLOSED=true`、Milvus Lite `.data/oir_memory_milvus.db`、DashScope/OpenAI-compatible `text-embedding-v4`、现有 DeepSeek router LLM。结果：`SMOKE_OK`，mem0 写入、PostgreSQL/OIR ledger、Milvus Lite `oir_memory_vectors` collection、`memory_context` 召回闭环通过，`events=6`。PostgreSQL 已使用独立 `oir` role + `oir` database，14 张 OIR 表 owner 均为 `oir`。
- 已新增 knowledge 侧 Milvus Lite 真实向量检索实现和 smoke helper：`scripts/smoke_knowledge_milvus_vector_search.py`。`MilvusKnowledgeVectorStore` 会调用阿里 embedding，写入/检索 `oir_knowledge_vectors`，并用 Milvus 返回的 `chunk_id` 回填 PostgreSQL canonical `knowledge_chunks`。
- 2026-07-09 knowledge 侧真实 smoke 已通过：真实 PostgreSQL `oir` role + `oir` database、Milvus Lite `.data/oir_knowledge_milvus.db`、`oir_knowledge_vectors`、DashScope/OpenAI-compatible `text-embedding-v4`、1024 维。结果：`SMOKE_OK`，写入 2 个 knowledge chunk，Milvus 语义检索首位命中目标 chunk，`knowledge_retrieval_logs` 写入 1 条。

## 2. 原始测试结论

结论：不通过，暂不建议验收。

原因：

- 原有核心回归通过，说明 schema、主流程、memory/knowledge 基本召回、Evidence Provider、前端构建等主路径已有基础可用性。
- 按测试方案补充的边界用例发现 3 个 P0/P1 问题：`max_items=0` 被回落为默认值、controlled retrieval 同样忽略 0、direct invoke 在只带部分 context 时跳过缺失 context 组装。
- `storage_backend=database` 下 memory/knowledge 运行时仍使用内存仓库，无法证明 Agent context memory/knowledge 的数据库持久化闭环。
- ruff lint 和 format check 未通过，质量门禁不满足退出标准。
- PostgreSQL、mem0、Milvus 真实基础设施未完成可验收闭环，当前只验证了内存环境和 SQLite 表/字段层面的能力。

## 3. 测试范围

已覆盖：

- Agent context schema 与配置解析。
- Memory recall、write candidate、TTL、cleanup、debug 可见性。
- Knowledge source policy、citation、empty、timeout、retrieval log。
- AgentContextAssemblyService 的 route/invoke context 注入。
- Controlled retrieval 模板变量白名单与 source denied。
- Evidence Provider strong/weak fixed question 行为。
- API 基本合约与部分 422 边界。
- SQLite legacy migration 的 `agent_definitions.context_text` 字段补齐。
- 前端 debug/admin 相关改动的 test/build 冒烟。

未完成或无法证明：

- 50 并发 route/invoke 下 context 不串用户/租户/session 的压力验证。
- debug/admin 输出的完整敏感字段脱敏扫描。

## 4. 新增补充测试

新增文件：`tests/test_agent_context_memory_knowledge_edge_cases.py`

补充用例：

| 用例 | 优先级 | 结果 | 目的 |
| --- | --- | --- | --- |
| `test_prefetch_max_items_zero_does_not_fallback_to_default` | P0 | 原失败，复测通过 | 验证 memory/knowledge prefetch 的 `max_items=0` 不触发检索 |
| `test_controlled_retrieval_max_items_zero_does_not_search` | P0 | 原失败，复测通过 | 验证 controlled retrieval 的 `max_items=0` 不触发检索 |
| `test_direct_invoke_with_partial_existing_context_assembles_missing_context` | P0 | 原失败，复测通过 | 验证 direct invoke 只带部分 context 时仍补齐缺失 context |
| `test_caller_type_admin_does_not_bypass_source_policy` | P0 | 通过 | 验证 caller_type 伪造成 admin 不绕过 source policy |
| `test_knowledge_search_api_rejects_invalid_boundary_values` | P1 | 通过 | 验证 `/knowledge/search` 的 `top_k=51` 和非法 caller_type 返回 422 |
| `test_memory_api_rejects_invalid_boundary_values` | P1 | 通过 | 验证 `/memories/recall` 的 `max_items=51` 和 write 缺 user_id 返回 422 |

## 5. 原始执行记录

| 命令 | 结果 | 说明 |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/test_agent_context_memory_knowledge.py tests/test_api.py tests/test_routing_invocation.py tests/test_database_migrations.py` | 通过 | 31 passed，1 warning |
| `.venv/bin/python -m pytest tests/test_agent_context_memory_knowledge_edge_cases.py` | 失败 | 3 failed，3 passed，1 warning |
| `.venv/bin/python -m pytest` | 失败 | 82 passed，3 failed，1 warning；失败均来自新增边界测试 |
| `.venv/bin/python -m ruff check tests/test_agent_context_memory_knowledge_edge_cases.py` | 通过 | 新增测试文件自身 lint 通过 |
| `.venv/bin/python -m ruff check .` | 失败 | 5 个 import 排序问题 |
| `.venv/bin/python -m ruff format --check .` | 失败 | 12 个文件需要格式化 |
| `openspec validate add-agent-context-memory-knowledge --strict` | 通过 | OpenSpec change 校验通过 |
| `npm run test` | 通过 | web：1 个测试文件，5 tests passed |
| `npm run build` | 通过 | web：TypeScript 与 Vite build 通过 |

## 6. 原始关键缺陷

### BUG-ACMK-001：`max_items=0` 在 prefetch 链路被忽略

级别：Critical
优先级：P0
影响用例：`test_prefetch_max_items_zero_does_not_fallback_to_default`

复现：

1. Agent 配置 `context.memory.mode=prefetch` 且 `max_items=0`。
2. Agent 配置 `context.knowledge.mode=prefetch` 且 `max_items=0`。
3. memory/knowledge repository 中预置可命中数据。
4. 调用 `AgentContextAssemblyService.assemble()`。

期望：

- `memory_context.items=[]`
- `knowledge_context.items=[]`
- invocation input 中两个 context 的 items 均为空

实际：

- memory_context 返回 1 条 memory。
- knowledge_context 返回 1 条 knowledge item。

定位：

- `app/services/agent_context_service.py:143`
- `app/services/agent_context_service.py:175`

原因判断：

- 代码使用 `config.max_items or self.settings.*_default_max_items`，合法值 `0` 被当作 falsy 回落到默认值。

风险：

- 用户显式配置“不取 context”仍然发生召回，可能造成隐私、成本和行为不可控问题。

### BUG-ACMK-002：`max_items=0` 在 controlled retrieval 链路被忽略

级别：Critical
优先级：P0
影响用例：`test_controlled_retrieval_max_items_zero_does_not_search`

复现：

1. Agent 配置 `context.knowledge.mode=controlled_retrieval`。
2. 配置 `max_items=0` 和合法 query template。
3. knowledge repository 中预置可命中 chunk。
4. 调用 `controlled_knowledge_retrieval()`。

期望：

- controlled retrieval 不返回 items。

实际：

- 返回了 `docs` source 下的 knowledge item。

定位：

- `app/services/agent_context_service.py:98`

原因判断：

- controlled retrieval 同样使用 `config.max_items or self.settings.knowledge_default_max_items`。

风险：

- 固定工作流节点显式限制为 0 时仍检索知识，违背声明式上下文契约。

### BUG-ACMK-003：direct invoke 只带部分 context 时缺失 context 不会被补齐

级别：Major
优先级：P0
影响用例：`test_direct_invoke_with_partial_existing_context_assembles_missing_context`

复现：

1. Agent 同时配置 memory prefetch 和 knowledge prefetch。
2. direct invoke 输入中只携带 `memory_context`。
3. knowledge repository 中预置可命中 chunk。
4. 调用 `InvocationService.invoke_agent()`。

期望：

- 复用已有 `memory_context`。
- 继续组装缺失的 `knowledge_context`。
- run.input 中同时包含两个稳定 context 字段。

实际：

- run.input 只有 `memory_context`，没有 `knowledge_context`。

定位：

- `app/services/invocation_service.py:169`
- `app/services/invocation_service.py:189`

原因判断：

- 当前逻辑只要输入中存在任意一个 context 字段，就跳过整个 AgentContextAssemblyService。

风险：

- route preview、direct invoke、host 自带 context 的行为不一致。
- 下游 Agent 可能收到不完整 context，排查时也缺少稳定字段。

## 7. 重要风险

### RISK-ACMK-004：`storage_backend=database` 未接入 memory/knowledge 数据库仓库

修复状态：已修复，已补充 SQLite database-backed context repository round-trip 测试。mem0 memory/OIR ledger 已在真实 PostgreSQL 实例上通过冒烟验证；knowledge metadata/log 已在真实 PostgreSQL `oir` database 中通过 Milvus smoke 验证。

级别：Critical
优先级：P0

观察：

- `app/db/models.py` 已定义 `memory_items`、`memory_events`、`knowledge_sources`、`knowledge_chunks`、`knowledge_retrieval_logs`。
- 但 `app/dependencies.py:76` 到 `app/dependencies.py:80` 的 `get_context_repository_bundle()` 始终返回 `MemoryItemRepository()` 和 `KnowledgeRepository()`。

风险：

- 即使 `storage_backend=database`，memory/knowledge 运行时数据仍不一定进入数据库。
- 当前测试只证明了表结构存在和 agent context 字段迁移，不证明 memory/knowledge 元数据、召回日志和 debug 状态可持久化。

验收建议：

- SQLite database-backed repository 已通过 round trip。mem0 memory/OIR ledger 的 PostgreSQL-backed metadata 已通过真实 smoke；knowledge source/chunk/log 已通过真实 PostgreSQL + Milvus Lite smoke。

### RISK-ACMK-005：Milvus 后端不是可验收的真实向量检索闭环

级别：Major
优先级：P1
状态：已通过本轮实现与真实 smoke 缓解；连接失败、权限错误和异常降级仍可继续补充专项测试。

观察：

- `MilvusKnowledgeVectorStore` 已改为使用 OpenAI-compatible 阿里 embedding 生成 query/chunk 向量，使用 Milvus Lite `oir_knowledge_vectors` 写入和 search，并通过 `chunk_id` 回填 PostgreSQL canonical `knowledge_chunks`。

风险：

- 已证明 `knowledge_vector_backend=milvus` 的真实 collection、embedding、向量检索和 canonical chunk 回填闭环；仍未覆盖连接失败和权限错误专项场景。

复核结果：

- 2026-07-09 真实 smoke 已确认 PostgreSQL `knowledge_sources` / `knowledge_chunks` / `knowledge_retrieval_logs`、Milvus Lite `.data/oir_knowledge_milvus.db`、`oir_knowledge_vectors`、阿里 embedding `text-embedding-v4` / 1024 维可形成 knowledge 检索闭环。
- 本次验收没有写入 `oac_knowledge_chunks`，保留 IRS 到 OIR 双 collection 过渡边界。

### RISK-ACMK-006：mem0 异常 fallback 可能掩盖生产配置失败

级别：Major
优先级：P1
状态：已通过 `integrate-mem0-memory-loop` 代码路径缓解，并已通过真实基础设施 smoke 复核。

观察：

- 新实现中 local fallback 会记录 `degraded`、`last_error`、`mem0_*` history event，并在 `/api/v1/memories/debug` 暴露。
- `MEMORY_MEM0_FAIL_CLOSED=true` 或 `APP_ENV!=local` 默认 fail-closed 时，mem0 add/search/delete 失败会返回结构化错误或 rejected write decision，不再静默报告成功。

复核结果：

- 2026-07-09 真实 smoke 已确认 mem0 SDK、Milvus Lite 文件 URI、阿里 embedding base URL、1024 维配置、现有 DeepSeek router LLM 和 PostgreSQL OIR ledger 可形成写入与召回闭环。
- 复核中发现并修复两个真实兼容点：Milvus Lite collection 初始化后需要显式 `load_collection()`；mem0 Milvus search 在 Milvus Lite 下需要把 `output_fields=["*"]` 转为显式 `["id", "metadata"]`，否则 semantic search 虽命中但 payload 为空。

## 8. 安全与权限结论

已通过：

- source role policy 基本拒绝正常。
- caller_type 伪造成 `admin` 不会自动绕过 source policy。
- requested source 缺失或拒绝会进入 `denied_source_ids`。
- fixed question strong override 在目标不可用时已有拒绝测试。

仍需补强：

- Agent 声明外 source 的硬边界需要更多端到端测试。
- debug/admin 响应尚未做完整敏感字段扫描。
- `max_items=0` 问题已修复并通过边界测试。

## 9. 端到端结论

通过：

- route preview 与 direct invoke 在无已有 context 的情况下可以注入结构化 memory_context 和 knowledge_context。
- invoke_from_route 复用 preview input 的设计可避免明显重复预取。
- Evidence Provider strong/weak fixed question 的核心路由行为通过既有测试。

原始不通过项：

- direct invoke 输入只带部分 context 时，缺失的一侧不会补齐。修复状态：已通过复测。
- `max_items=0` 端到端语义不一致，schema 允许但执行层不尊重。修复状态：已通过复测。

## 10. 验收建议

2026-07-09 修复复测后，建议进入有条件验收：内存环境、SQLite database-backed repository、mem0 memory 真实闭环、PostgreSQL OIR ledger、Milvus Lite memory collection、knowledge 侧 Milvus Lite 真实向量检索、knowledge PostgreSQL source/chunk/log round trip、后端回归、静态检查、OpenSpec、前端测试与构建均已通过。并发隔离和连接失败专项仍属于需要单独验证的条件项。

已完成修复项：

1. 将 `config.max_items or default` 改为显式 `config.max_items if config.max_items is not None else default` 或等价逻辑，并保留 `0` 语义。
2. controlled retrieval 使用同样的 `max_items=0` 语义。
3. `InvocationService._with_agent_context()` 支持 partial context：已有的一侧复用，缺失的一侧按 Agent 声明补齐。
4. 为 memory/knowledge 增加 database-backed repository，并补充 SQLite round trip 测试。
5. 修复 ruff lint 和 format check。
6. 接入真实 mem0 SDK + Milvus Lite memory collection + 阿里 embedding + DeepSeek router LLM，并补齐 Milvus Lite load/output_fields 兼容。
7. 接入 knowledge 侧 Milvus Lite 真实向量写入/检索，使用阿里 embedding，并通过 Milvus `chunk_id` 回填 PostgreSQL canonical chunk。

仍需外部环境验证：

1. 50 并发 route/invoke 下 context 隔离压力验证。
2. Milvus/embedding/PostgreSQL 连接失败、权限错误和超时的专项验收。

复测命令：

```bash
.venv/bin/python -m pytest tests/test_agent_context_memory_knowledge_edge_cases.py
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
openspec validate add-agent-context-memory-knowledge --strict
.venv/bin/python scripts/smoke_knowledge_milvus_vector_search.py
npm run test
npm run build
```

其中 `npm run test` 和 `npm run build` 需要在 `web/` 目录执行。
