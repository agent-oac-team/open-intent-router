## Why

现有 M5/M6 已经定义了 `memory_context`、`MemoryService`、mem0 adapter 边界和数据库仓储，但测试报告仍把真实 PostgreSQL、mem0、Milvus 闭环列为未验收项。现在需要把 mem0 从“可选占位策略层”推进到可配置、可观测、可失败显式化的真实记忆能力，并在复用 IRS 本地 PostgreSQL、同时以 Milvus Lite 承接本地向量索引时保持 OIR 后续替代 IRS 的迁移路径清晰。

## What Changes

- 接入真实 mem0 OSS Python 客户端，使用 `Memory.from_config()` 初始化，并支持通过配置指定 Milvus Lite memory collection、embedding 维度、LLM、embedder、history store 等参数。
- 本地 Milvus 统一使用 Milvus Lite；`oir_memory_vectors` 使用独立 Milvus Lite 存储，不依赖 standalone Milvus 服务。
- 接入 PostgreSQL history：OIR 必须把 mem0 add/search/update/delete 的 history、外部 ID 映射和治理事件写入 PostgreSQL；如果当前 mem0 Python SDK 无直接 PostgreSQL history 配置，则由 OIR adapter/history ledger 层承接。
- 记忆 embedding 沿用 IRS 的阿里 embedding 配置，包括 OpenAI-compatible base URL、model、API key 和 embedding 维度。
- 将 OIR 的记忆元数据、审计事件和治理状态继续保存在 OIR 自有 PostgreSQL 表中；mem0 只作为 extraction、semantic search、merge/update 的策略层。
- 复用本地 PostgreSQL 基础设施，并统一使用 Milvus Lite 承接本地向量索引；记忆向量使用独立 `oir_memory_vectors` collection，不与知识向量共用 collection。
- 将 mem0 adapter 的失败策略从“静默 fallback”改成可配置：本地开发可降级，生产/验收配置必须暴露错误并记录健康状态。
- 增加真实闭环验收路径：memory write candidate -> OIR policy -> mem0 add -> OIR metadata/event -> mem0 search -> `memory_context` -> route/invoke handoff。
- 知识向量保持两个 collection 过渡：IRS 现有 `oac_knowledge_chunks` 与 OIR 后续 `oir_knowledge_vectors` 并行；本 change 只建立迁移护栏，不实现完整知识迁移。
- 文档全部使用中文，明确本地 `.env` 配置、验收命令、故障排查和 collection 迁移约束。

## Capabilities

### New Capabilities

- `mem0-memory-loop`: 真实 mem0 记忆闭环、配置、失败策略、审计可观测和验收要求。
- `knowledge-vector-transition`: `oac_knowledge_chunks` 与 `oir_knowledge_vectors` 双 collection 过渡、可迁移性约束和知识索引边界。

### Modified Capabilities

- 无。当前仓库尚未归档 M5/M6 到 `openspec/specs/`，本次以新能力规格承接未验收闭环与迁移护栏。

## Impact

- 后端依赖：新增 mem0 OSS 客户端、Milvus Lite 运行依赖和 PostgreSQL history/ledger 支持；保持测试环境可以不依赖真实外部服务。
- 配置：扩展 `Settings`、`.env.example` 和中文文档，覆盖 `MEMORY_STRATEGY_PROVIDER=mem0`、mem0 JSON/分项配置、Milvus Lite URI/collection、PostgreSQL history、阿里 embedding、生产失败策略。
- 服务：强化 `Mem0MemoryAdapter` 初始化、add/search/delete、错误处理、返回字段映射、健康检查和 debug metadata。
- 数据库：OIR 自有 `memory_items`、`memory_events` 继续作为治理与审计事实来源；PostgreSQL 实例可复用 IRS 本地实例，但 schema/table 边界必须明确。
- Milvus：记忆 collection 与知识 collection 分离；`oir_memory_vectors`、`oac_knowledge_chunks`、`oir_knowledge_vectors` 的职责和迁移状态需要文档化。
- 测试：新增单元测试、fake mem0 adapter 测试、配置解析测试、失败策略测试，以及可选真实 PostgreSQL、Milvus Lite、mem0、阿里 embedding 冒烟测试。
