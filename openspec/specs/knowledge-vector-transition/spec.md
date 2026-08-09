# knowledge-vector-transition Specification

## Purpose
TBD - created by archiving change integrate-mem0-memory-loop. Update Purpose after archive.
## Requirements
### Requirement: 记忆向量和知识向量使用独立 collection
系统 SHALL 在 IRS 到 OIR 的过渡期内，把记忆向量和知识向量保存在独立 Milvus collection 中。

#### Scenario: mem0 记忆使用 memory collection
- **WHEN** OIR 写入或搜索 mem0-backed memory
- **THEN** 操作目标是配置的 memory collection，例如 `oir_memory_vectors`，而不是 `oac_knowledge_chunks` 或 `oir_knowledge_vectors`

#### Scenario: 知识 collection 保持可区分
- **WHEN** OIR 引用 IRS 知识或未来 OIR 知识向量
- **THEN** `oac_knowledge_chunks` 和 `oir_knowledge_vectors` 在配置、日志和迁移文档中保持可单独识别

### Requirement: Milvus collection 是派生索引
系统 SHALL 将 Milvus 知识 collection 视为可重建的派生索引，而不是 canonical knowledge storage。

#### Scenario: 必须有关联的 canonical knowledge metadata
- **WHEN** 从 `oac_knowledge_chunks` 或 `oir_knowledge_vectors` 返回知识向量结果
- **THEN** 系统必须能在向量之外关联到 canonical asset/chunk metadata、source information 和 citation data

#### Scenario: 向量假设不兼容时必须 reindex
- **WHEN** 旧知识索引和新知识索引的 embedding model、embedding dimension、chunking strategy、vector schema 或 filter semantics 不一致
- **THEN** 迁移路径 MUST 要求基于 canonical chunks 重新索引，而不是复制既有向量并假装二者等价

### Requirement: 双 collection 过渡保留 provenance
系统 SHALL 在 IRS 和 OIR 知识 collection 共存期间保留 collection provenance。

#### Scenario: 查询结果包含 collection provenance
- **WHEN** 检索流程读取 `oac_knowledge_chunks` 或 `oir_knowledge_vectors`
- **THEN** debug metadata、logs 或 citations 标明每个结果来自哪个 collection

#### Scenario: 结果不能在丢失来源标记后合并
- **WHEN** 未来检索流程同时查询旧知识 collection 和新知识 collection
- **THEN** 系统 MUST NOT 将 hits 合并成丢失 collection、asset、chunk 或 citation provenance 的未标记结果列表

### Requirement: 知识切换前必须规划迁移映射
系统 SHALL 在用 OIR 知识向量替换 IRS 知识向量之前定义 mapping 或 manifest。

#### Scenario: Mapping 记录新旧身份
- **WHEN** 知识 chunk 从 IRS-compatible storage 迁移或重新索引到 OIR storage
- **THEN** migration mapping 记录 old asset/chunk/index identifiers、new asset/chunk/index identifiers、collection names、embedding model、dimension 和 migration status

#### Scenario: Cutover 可以被审计
- **WHEN** OIR 停止为某个 source 使用 `oac_knowledge_chunks`
- **THEN** 维护者可以检查文档或迁移 metadata，确认该 source 是已重新索引、已跳过还是被有意退役

### Requirement: 知识过渡不阻塞 mem0 记忆闭环
系统 SHALL 将知识 collection 迁移工作与 mem0 记忆闭环实现解耦。

#### Scenario: 记忆闭环先于知识迁移交付
- **WHEN** `integrate-mem0-memory-loop` 被实现
- **THEN** mem0 memory add/search 和 `memory_context` assembly 可以在不要求 `oac_knowledge_chunks` 迁入 `oir_knowledge_vectors` 的情况下验收

#### Scenario: 知识护栏被文档化
- **WHEN** 记忆闭环交付
- **THEN** 文档描述双 collection 过渡约束，避免后续知识迁移工作破坏 provenance 或 reindex 要求

