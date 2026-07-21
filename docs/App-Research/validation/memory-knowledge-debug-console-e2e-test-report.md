# 记忆与知识库调试台端到端测试报告

日期：2026-07-09
测试方案：`docs/App-Research/validation/memory-knowledge-debug-console-e2e-test-plan.md`
适用变更：`add-memory-knowledge-debug-console`

## 1. 结论

本轮已完成真实本地环境验收：

- PostgreSQL 使用 OIR 专属库，memory/knowledge canonical 数据均落库。
- mem0 SDK + Milvus Lite `oir_memory_vectors` 记忆写入与召回闭环通过。
- Knowledge PostgreSQL canonical chunks + Milvus Lite `oir_knowledge_vectors` 向量检索闭环通过。
- 前端每轮对话 badge、右侧 Memory/Knowledge tab、历史 turn 选择和只读管理页均通过。
- 注册表当前为 file backend，共 4 个 Agent，实际 `/api/v1/agents` 与 `config/agents.example.yaml` 一致。

## 2. 本轮新增测试辅助

新增 helper：

```bash
.venv/bin/python scripts/seed_memory_knowledge_debug_console_data.py
```

helper 行为：

- 默认要求 `STORAGE_BACKEND=database` 和 `KNOWLEDGE_VECTOR_BACKEND=milvus`，避免造到内存仓库造成假通过。
- 写入 `e2e_user_a/e2e_tenant_a`、`e2e_user_b/e2e_tenant_b` 的 memory candidates。
- 写入 `wealth_product_docs`、`risk_policy_docs`、`visit_playbook`、`e2e_admin_docs`、`e2e_disabled_docs`、`e2e_tenant_b_docs`。
- 对 7 个 E2E knowledge chunks 执行 Milvus Lite upsert。
- 自动验证 memory single-scope、memory multi-scope、knowledge wealth hit、knowledge denied source。

最终 helper verification：

| 项目 | 结果 |
| --- | --- |
| `memory_single_scope` | `ok`，命中 `mem_5385e0ee6e8a4b52b6a1e9cbf79b9260` |
| `memory_multi_scope` | `ok`，命中 user preference、stable fact、task memory 共 3 条 |
| `knowledge_wealth` | `ok`，首位命中 `e2e_chunk_wealth_liquidity` |
| `knowledge_denied` | `empty`，denied `e2e_admin_docs`、`e2e_disabled_docs`、`missing_source` |

## 3. 测试中发现并修复的问题

### 3.1 mem0 多 scope filter 不兼容

现象：

- 历史 UI smoke 和本轮初测均出现 mem0 search error。
- 旧实现向 mem0 下发 `scope: {"in": [...]}`。
- 改成 `AND/OR` 后，当前 OSS SDK 仍报 `filters must contain at least one of: user_id, agent_id, run_id`，因为它要求实体字段在顶层。

修复：

- 多 scope 不再构造复合 filter。
- `Mem0MemoryAdapter.search()` 按 scope 拆成多次 primitive filter search，再合并去重。
- 不再向 mem0 下推 `agent_id`，避免通用用户记忆被 `agent_id=<当前 Agent>` 过滤掉。

验证：

- 单测新增 `test_mem0_search_filter_sets_expand_multiple_scopes`。
- 真实 helper `memory_multi_scope` 通过。
- HTTP `/api/v1/memories/recall` 多 scope 返回 `status=ok`、3 条 items。

### 3.2 prefetch timeout 对真实 mem0/embedding 过紧

现象：

- 直接 `/memories/recall` 成功，但 `route-and-invoke` 中 `script_writer` 首次出现 `memory_context.status=timeout`。
- 并发 cold knowledge search 也曾触发 `knowledge_search_timeout`。

修复：

- `MEMORY_PREFETCH_TIMEOUT_SECONDS` 默认从 `0.8` 调整为 `3.0`。
- `KNOWLEDGE_PREFETCH_TIMEOUT_SECONDS` 默认从 `1.5` 调整为 `5.0`。
- `.env.example` 和本地 `.env` 已同步非敏感 timeout 配置。

验证：

- 重启后 `/api/v1/runtime/config` 显示 memory timeout `3.0`、knowledge timeout `5.0`。
- `script_writer` route-and-invoke 返回 `memory_context_status=ok`、`memory_item_count=3`。
- `wealth_knowledge` route-and-invoke 返回 `memory_context_status=ok`、`knowledge_context_status=ok`。

## 4. HTTP 验收结果

### 4.1 Runtime

`GET /api/v1/runtime/config`：

- `storage_backend=database`
- `registry_backend=file`
- `registry_agent_count=4`
- `memory_strategy_provider=mem0`
- `memory_mem0_collection=oir_memory_vectors`
- `memory_mem0_health_status=ok`
- `knowledge_vector_backend=milvus`
- `knowledge_milvus_collection=oir_knowledge_vectors`
- 未暴露真实 API key，只显示 configured 状态。

### 4.2 Memory Debug / Recall

`GET /api/v1/memories/debug?user_id=e2e_user_a&tenant_id=e2e_tenant_a&scopes=user_preference,stable_fact,task_memory`：

- 返回 3 条 active memory。
- `task_memory` TTL 为 `2026-07-23`。
- debug events 中可看到多 scope search 记录为 3 个 primitive filter sets。

`POST /api/v1/memories/recall`：

- 请求 scopes 为 `user_preference,stable_fact,task_memory`。
- 返回 `status=ok`、3 条 items。
- mem0 health `ok`，collection `oir_memory_vectors`。

### 4.3 Knowledge Debug / Search

`GET /api/v1/knowledge/debug`：

- 可见本轮 E2E 6 个 source、7 个 chunk。
- 管理页明确提示 PostgreSQL source/chunk/log 为 canonical，Milvus 只是向量索引。

`POST /api/v1/knowledge/search`：

- `wealth_product_docs,risk_policy_docs` 查询返回 `status=ok`。
- 首位命中 `e2e_chunk_wealth_liquidity`。
- citations 覆盖 `wealth_product_docs` 与 `risk_policy_docs`。
- `e2e_admin_docs,e2e_disabled_docs,missing_source` 对 operator 用户返回 empty，denied source IDs 完整。

### 4.4 Route And Invoke

| 输入 | Agent | Memory | Knowledge | 结果 |
| --- | --- | --- | --- | --- |
| 客户邀约话术，强调稳健/流动性/不要销售化 | `script_writer` | `ok`，3 条 | `disabled` | completed |
| 久期风险、净值波动、流动性缓冲 | `wealth_knowledge` | `ok`，2 条 | `ok`，3 条 citations | completed |
| 客户画像在哪里 | `system_usage_guide` | `disabled` | `disabled` | UI handoff completed |
| 访前准备，目标解释稳健配置 | `visit_preparation` | `ok`，3 条 | `disabled` | completed |

`visit_preparation` 的 knowledge 为 disabled 符合当前定义：它配置的是 `controlled_retrieval`，普通 route/invoke 不会假装已有 knowledge context。

## 5. 前端 UI 验收结果

浏览器测试台：`http://127.0.0.1:5173/`

| 场景 | 结果 |
| --- | --- |
| 第一轮 `script_writer` | 聊天气泡显示 `Memory 3 / ok`、`Knowledge 0 / disabled`、`Citations 0`、`Denied 0` |
| 第二轮 `wealth_knowledge` | 聊天气泡显示 `Memory 2 / ok`、`Knowledge 3 / ok`、`Citations 3`、`Denied 0` |
| 右侧 Knowledge tab | 显示本轮 `items: 3`、source IDs、vector backend、3 个 chunk 和 citations |
| 历史 turn 选择 | 点击第一轮后右侧 Memory tab 切回第一轮 `items: 3` |
| 只读 Knowledge 管理页 | 显示 `oir_knowledge_vectors`、canonical 边界说明、E2E sources/chunks/logs |

## 6. 已知注意事项

- helper 直连 mem0/Milvus Lite 时，如果后端进程已经持有 `.data/oir_memory_milvus.db`，会报 Milvus Lite lock。处理方式：先停止后端，再跑 helper；或后续扩展为 memory 通过后端 API 造数。
- 未安装 `mem0ai[nlp]` 时会提示 spaCy lemma/full model 缺失，本轮不影响闭环。
- 浏览器插件出现过 Statsig 网络超时日志，属于浏览器插件遥测请求，不影响本地 UI 验收。
- 后端停止时出现过 Milvus/grpc `too_many_pings` 日志，本轮 HTTP/UI 功能未受影响，可作为后续稳定性观察项。

## 7. 回归命令

```bash
.venv/bin/python -m pytest
cd web && npm run test
cd web && npm run build
openspec validate add-memory-knowledge-debug-console --strict
openspec validate --all --strict
```

结果：

- 后端：97 passed，1 个 Starlette/httpx deprecation warning。
- 前端：9 passed。
- 前端 build：通过。
- OpenSpec 当前 change：通过。
- OpenSpec 全量：31 passed，0 failed。
