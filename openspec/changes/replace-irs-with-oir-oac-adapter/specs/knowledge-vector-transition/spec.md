## MODIFIED Requirements

### Requirement: Milvus collection 是派生索引
系统 SHALL 将 Milvus 知识 collection 视为可基于 OIR PostgreSQL canonical Asset/Chunk 与 Migration Manifest 重建的派生索引，而不是 canonical knowledge storage。

#### Scenario: 必须有关联的 canonical knowledge metadata
- **WHEN** 从 `oac_knowledge_chunks` 或 `oir_knowledge_vectors` 返回知识向量结果
- **THEN** 系统必须能在向量之外关联到 canonical asset/chunk metadata、source information 和 citation data

#### Scenario: 向量假设不兼容时必须 reindex
- **WHEN** 旧知识索引和新知识索引的 embedding model、embedding dimension、chunking strategy、vector schema 或 filter semantics 不一致
- **THEN** 迁移路径 MUST 要求基于 OIR canonical chunks 重新索引，而不是复制既有向量并假装二者等价

#### Scenario: Exact Read 在 Milvus 不可用时执行
- **WHEN** 调用方按已知 Asset/Chunk/source_ref 读取授权 canonical 内容且 Milvus 不可用
- **THEN** 系统从 PostgreSQL 返回结果，不要求向量索引可用

### Requirement: 知识切换前必须规划迁移映射
系统 SHALL 在用 OIR 知识向量替换 IRS 知识向量之前创建 Migration Manifest，以原始文件与 OIR canonical Source/Asset/Chunk/Vector 为主轴记录重建身份、版本、哈希、provenance、状态和验证结果。

#### Scenario: Manifest 记录原始文件到新索引的关系
- **WHEN** 原始文件被解析和重新索引到 OIR storage
- **THEN** Migration Manifest 记录 source file/hash/range、parser/chunking/embedding/vector schema 版本、new asset/chunk/index identifiers、collection 名称、migration status 和 validation status

#### Scenario: IRS canonical 数据只用于对账
- **WHEN** IRS PostgreSQL 或 HTTP Read/Assets 在迁移期可用
- **THEN** 系统可用其对账数量、内容和契约结果，但 MUST NOT 用它替代原始文件重建或复制旧 Milvus 向量

#### Scenario: Cutover 可以被审计
- **WHEN** OIR 停止为某个 source 使用 `oac_knowledge_chunks`
- **THEN** 维护者可以通过 Manifest 确认该 source 已重新索引并验证、已跳过、已延后或被有意退役

## ADDED Requirements

### Requirement: OIR 知识必须从已确认原始文件重建
系统 SHALL 以 `/Users/lijingtong/project/data/内容生产` 的 6 份工作簿作为首批 OIR Knowledge 重建输入，并为每份文件生成完整 Manifest；`oac_knowledge_chunks` 中的旧向量 MUST NOT 被复制进 `oir_knowledge_vectors`。

#### Scenario: 导入非空工作簿
- **WHEN** 01、02、04、05 或 06 原始文件解析成功
- **THEN** 系统保存 canonical Asset/Chunk/source_ref/hash 并使用当前已记录 Embedding 配置写入 `oir_knowledge_vectors`

#### Scenario: 导入空客群工作簿
- **WHEN** 03 文件仅包含表头
- **THEN** Manifest 标记 empty/deferred，保留稳定 Asset 槽位，不写入伪 Chunk 或向量

### Requirement: 知识迁移以业务语义与权限一致性验收
系统 SHALL 通过 Golden Queries、Exact Read、业务字段对账和权限负向用例验证核心事实、适用条件、金额/期限/阈值和来源语义，而不要求新旧 Chunk 文本、ID 或向量物理同构。

#### Scenario: 重新切块但业务语义一致
- **WHEN** OIR Chunk 边界与 IRS 不同，但授权查询的核心事实、条件、数值和 source_ref 语义与原始文件一致
- **THEN** 该差异可按非物理同构记录并通过语义验收

#### Scenario: OIR 结果改变核心业务事实或权限
- **WHEN** OIR 查询结果与原始文件在核心事实上矛盾或暴露未授权内容
- **THEN** 知识切换门禁失败，且 Manifest validation status 不得标记为 validated
