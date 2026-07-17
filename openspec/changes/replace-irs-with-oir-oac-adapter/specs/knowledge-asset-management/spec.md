## ADDED Requirements

### Requirement: PostgreSQL 是 Knowledge Asset 与 Chunk 的 canonical store
系统 SHALL 在 PostgreSQL 中持久化 Knowledge Asset、Chunk、Asset Group、Import Job、权限、状态、source reference、citation、内容哈希与版本元数据，并将 Milvus 视为可重建的派生索引。

#### Scenario: 写入可检索 Chunk
- **WHEN** 一条通过解析和治理校验的 Chunk 进入索引阶段
- **THEN** 系统先在 PostgreSQL 保存 canonical Chunk 与 provenance，再将其向量写入独立 Knowledge collection

#### Scenario: Milvus 索引丢失
- **WHEN** `oir_knowledge_vectors` 不可用或需要重建
- **THEN** 系统能基于 PostgreSQL canonical Chunk 和 Manifest 重建向量，不把向量库当作唯一事实源

### Requirement: Knowledge Search 实施通用身份、用途、范围与权限治理
系统 SHALL 接受包含 caller/consumer、purpose、tenant、user、subject、source scope 和返回预算的通用查询，并在模型或向量结果暴露前应用状态、敏感级和 access policy。

#### Scenario: 用户无权访问 restricted Asset
- **WHEN** 召回候选属于用户不具备权限的 restricted Asset
- **THEN** 系统在生成模型上下文或 API evidence 前过滤该候选并记录脱敏 policy outcome

#### Scenario: Asset 不可搜索
- **WHEN** Asset/Chunk 为 secret、disabled、deleted 或 failed
- **THEN** 其 MUST NOT 进入有效 evidence，且检索结果保留可区分的过滤原因

### Requirement: Grouped Search 保留稳定资产组与组内结构
系统 SHALL 支持按 Asset Group 和稳定 Asset Key 查询，为每个资产独立返回 matched、evidence、warnings 和 trace，并在请求包含空资产时保留结构槽位。

#### Scenario: 指定 Asset Group 搜索
- **WHEN** 查询指定存在的 Asset Group 与 Asset Keys
- **THEN** 系统按组定义的稳定顺序返回各资产结果，每条 evidence 保留 asset/chunk/source/citation provenance

#### Scenario: 未命中资产要求保留
- **WHEN** grouped request 要求 include-empty 且某资产无有效 Chunk
- **THEN** 该稳定 Asset Key 仍出现在响应中，其 matched 为 false 且 evidence 为空

### Requirement: Exact Read 仅读取授权 canonical 数据
系统 SHALL 支持按单/多 Asset、单/多 Chunk 和 `asset_id + source_ref` 精确读取 PostgreSQL canonical 内容，并对每个 target 执行状态、权限与敏感级校验。

#### Scenario: Milvus 不可用时精确读取
- **WHEN** 授权调用方按已知 Asset/Chunk ID 读取且 Milvus 故障
- **THEN** 系统仍从 PostgreSQL 返回授权 canonical 内容，不执行向量检索

#### Scenario: 批量 Chunk 包含缺失或被过滤 ID
- **WHEN** 请求的 Chunk IDs 包含存在、缺失和无权目标
- **THEN** 系统尽量保持授权结果的请求顺序，并分别返回 missing 与 filtered 诊断而不泄漏无权正文

#### Scenario: source_ref 不唯一
- **WHEN** `asset_id + source_ref` 对应多个 canonical Chunk 且契约要求单一目标
- **THEN** 系统返回 ambiguous 诊断，不随机选择 Chunk

### Requirement: Knowledge Admin 使用可恢复的入库状态机
系统 SHALL 支持文件上传、校验、解析、切块、Embedding、索引、替换、重试、软删除和清理，并为 Asset 与 Import Job 持久化每个状态/阶段、warning 和失败原因。

#### Scenario: 支持的文件成功入库
- **WHEN** 符合扩展名、大小、content type 和解析资源限制的文件上传
- **THEN** 系统创建 Asset + Import Job，按 parsing/chunking/embedding/indexing 推进，成功后记录 indexed 与 chunk count

#### Scenario: 入库中途失败
- **WHEN** Parser、Embedding 或向量索引阶段失败
- **THEN** Job 保留 failed stage、结构化 warning/error 和可审计重试信息，未完成 Asset MUST NOT 进入有效检索

#### Scenario: 替换现有 Asset
- **WHEN** 有效上传指定 `replace_asset_id`
- **THEN** 系统在新版本完整通过后保持稳定 Asset ID 切换 canonical 内容，失败时不留下可检索幽灵 Chunk

### Requirement: Knowledge 入库与检索保留端到端 provenance
系统 SHALL 为每个 Asset/Chunk/Vector 保留原文件、工作表/页/行/段落引用、文件与内容哈希、Parser/Chunking/Embedding 版本、collection/index ID 和 citation。

#### Scenario: 从 Search Evidence 追溯原文件
- **WHEN** Search 返回一条 evidence
- **THEN** 维护者可通过 asset/chunk/source_ref/manifest 定位原文件范围和生成该索引的版本

#### Scenario: 重新切块后物理 ID 变化
- **WHEN** Parser 或 Chunking 升级产生新 Chunk IDs
- **THEN** 系统通过 Manifest 保留新旧版本、稳定 Asset 和 source_ref 关系，不声称物理 Chunk 同构

### Requirement: 原始文件导入幂等且不伪造空数据
系统 SHALL 使用原始文件 SHA-256 和导入版本保证重复导入幂等，并将仅有表头的资产标记为 empty/deferred 而不创建空白 Chunk。

#### Scenario: 相同文件重复导入
- **WHEN** 相同 file hash、Parser/Chunking/Embedding 版本的已成功文件再次导入
- **THEN** 系统返回已有或幂等 Manifest 结果，不重复创建 canonical Chunk 或向量

#### Scenario: 空客群表导入
- **WHEN** `03 客群表.xlsx` 只包含表头且无有效数据行
- **THEN** 系统保留资产槽位并记录 deferred，Chunk 与向量数量为零

### Requirement: Knowledge 查询和管理操作可观测且脱敏
系统 SHALL 记录 request/trace、consumer、purpose、scope、policy outcome、evidence IDs、warnings、延迟和导入阶段，但 MUST NOT 记录凭证或不必要的完整敏感正文。

#### Scenario: 管理员排查无命中
- **WHEN** 授权管理员按 trace ID 查看 Search 执行
- **THEN** 系统展示候选、过滤、warning 和耗时的脱敏摘要，足以区分真无命中与 Provider 故障
