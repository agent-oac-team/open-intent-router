## 1. 配置与依赖

- [x] 1.1 在 `pyproject.toml` 中增加 mem0/Milvus Lite 相关可选依赖，确保默认测试环境不强制连接真实外部服务。
- [x] 1.2 扩展 `app/core/config.py` 的 memory/mem0 配置字段，覆盖 provider、fail-closed、Milvus Lite URI、collection、PostgreSQL history、阿里 embedding、LLM/embedder 配置和 JSON override。
- [x] 1.3 更新 `.env.example`，用中文注释说明本地 Milvus Lite、PostgreSQL history、阿里 embedding、真实验收和生产 fail-closed 的推荐配置。
- [x] 1.4 增加配置解析测试，覆盖默认 repository strategy、mem0 显式字段、Milvus Lite URI、PostgreSQL history、阿里 embedding、`MEM0_CONFIG_JSON` override 和 fail-closed 默认值。

## 2. mem0 Client 与 Adapter

- [x] 2.1 为 mem0 初始化增加 client factory，使真实 `Memory.from_config()` 和测试 fake client 可注入。
- [x] 2.2 将 Settings 显式字段转换为 mem0 config，Milvus vector store 默认使用 Milvus Lite URI 和 `oir_memory_vectors` collection。
- [x] 2.3 接入 PostgreSQL history/ledger；若 mem0 SDK 没有直接 PostgreSQL history backend，则在 OIR adapter 层记录 canonical history。
- [x] 2.4 接入 IRS 阿里 embedding 配置，默认使用 OpenAI-compatible base URL、`text-embedding-v4` 和 1024 维。
- [x] 2.5 改造 `Mem0MemoryAdapter.search()`，使用 query、user、tenant、subject、scope、agent metadata filters 调用 mem0，并兼容 mem0 返回结构。
- [x] 2.6 改造 `Mem0MemoryAdapter.add()`，向 mem0 写入 content/messages 与 OIR governance metadata，并保存 `mem0_memory_id` 映射。
- [x] 2.7 改造 `Mem0MemoryAdapter.delete_many()`，优先使用 `mem0_memory_id` 或 OIR metadata 删除外部记忆记录。
- [x] 2.8 增加 adapter 单元测试，覆盖 add/search/delete、metadata 映射、返回字段兼容、PostgreSQL history 和 ID traceability。

## 3. 闭环治理与失败策略

- [x] 3.1 保持 `MemoryService.write_candidates()` 先执行 OIR policy，确认被拒绝候选不会调用 mem0。
- [x] 3.2 实现 local fallback 与 fail-closed 的分支：local fallback 必须记录 degraded 状态，fail-closed 必须返回结构化错误或 rejected 决策。
- [x] 3.3 将 mem0 add/search/delete 的异常写入 memory event、debug metadata 或日志，避免静默成功。
- [x] 3.4 确保 mem0 search 结果进入既有 `memory_context` schema，并继续经过 TTL、冲突检测和 Context Pack/单项长度限制。
- [x] 3.5 增加端到端服务测试，覆盖 write candidate -> mem0 add -> OIR metadata/event -> recall -> `memory_context`。

## 4. 可观测、Runtime 与健康检查

- [x] 4.1 扩展 `/api/v1/runtime/config` 或 debug metadata，展示 memory provider、mem0 collection、fail-closed、degraded 状态和最近错误摘要。
- [x] 4.2 扩展 `/api/v1/memories/debug`，在不暴露密钥的前提下显示 mem0 provider 状态、collection、外部 ID 映射和错误信息。
- [x] 4.3 增加 mem0 初始化/连接的轻量健康检查或 smoke helper，明确区分配置错误、依赖缺失、Milvus Lite 文件不可用、阿里 embedding 不可用和维度不匹配。
- [x] 4.4 增加安全测试，确认 debug/runtime 响应不会暴露 API key、Milvus token 或真实连接密码。

## 5. 知识 Collection 过渡护栏

- [x] 5.1 在中文文档中明确 `oir_memory_vectors`、`oac_knowledge_chunks`、`oir_knowledge_vectors` 的职责边界。
- [x] 5.2 文档化 `oac_knowledge_chunks` 与 `oir_knowledge_vectors` 双 collection 过渡策略，说明何时必须 reindex 而不是复制向量。
- [x] 5.3 为后续知识迁移定义 mapping/manifest 建议字段：old/new asset、chunk、index、collection、embedding model、dimension、migration status。
- [x] 5.4 确认本 change 的代码路径不会把 mem0 memory collection 与知识 collection 混用，并记录 Milvus Lite 文件与 collection 的对应关系。

## 6. 文档与验收

- [x] 6.1 更新 `docs/App-Desc/contracts/api.md` 或新增中文集成文档，说明 mem0 记忆闭环、Milvus Lite、PostgreSQL history、阿里 embedding、配置示例、失败策略和调试方式。
- [x] 6.2 更新测试报告或新增验收记录，标记真实 mem0、PostgreSQL、Milvus Lite、阿里 embedding 闭环的执行条件和结果记录格式。
- [x] 6.3 增加可选真实基础设施 smoke test 命令或脚本说明，验证 PostgreSQL、Milvus Lite 和阿里 embedding 可用时的 add/search 闭环。
- [x] 6.4 运行 `.venv/bin/python -m pytest`，确认默认无真实外部服务环境通过。
- [x] 6.5 运行 `.venv/bin/python -m ruff check .` 和 `.venv/bin/python -m ruff format --check .`。
- [x] 6.6 运行 `openspec validate integrate-mem0-memory-loop --strict`。
