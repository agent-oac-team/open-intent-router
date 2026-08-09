## MODIFIED Requirements

### Requirement: 记忆向量和知识向量使用独立 collection
系统 SHALL 将 OIR Memory Store 与待删除的 OIR Knowledge 副本保持独立命名空间、配置和
权限；清理 Knowledge Collection MUST NOT 读取、写入、重建或删除 Memory Collection。

#### Scenario: OIR Memory 使用显式 collection
- **WHEN** OIR 写入或搜索 Memory
- **THEN** 操作只使用显式 `MEMORY_*` 配置指向的既有 Memory Collection，不回退 Knowledge 配置

#### Scenario: 执行 Knowledge 清理
- **WHEN** 清理工具删除 OIR 专属 Knowledge Collection
- **THEN** Memory Collection、向量数量、Embedding 配置和 Recall 行为保持不变

### Requirement: Milvus collection 是派生索引
系统 SHALL 将 Knowledge Milvus Collection 的事实归属交给 `knowledge_sys`，并从 OIR
删除本地 Knowledge 派生索引；OIR Core MUST NOT 通过物理 Collection 直接检索 Knowledge。

#### Scenario: OIR 需要 Agent Knowledge
- **WHEN** Agent Context 需要检索外部 Knowledge
- **THEN** OIR 调用 Provider-neutral Knowledge Provider，不读取 `oac_knowledge_chunks` 或 `oir_knowledge_vectors`

#### Scenario: knowledge_sys 重建索引
- **WHEN** Knowledge 所有者需要重建向量
- **THEN** 该生命周期完全由 `knowledge_sys` 基于其 Canonical Data 执行，不要求 OIR 参与

### Requirement: 双 collection 过渡保留 provenance
系统 SHALL 在 OIR Knowledge 副本删除前以无正文 Manifest 记录待清理 Schema、数量与
聚合 Hash；该记录 MUST NOT 成为可恢复的 Knowledge 备份或长期正文副本。

#### Scenario: 执行清理 Dry Run
- **WHEN** Dry Run 盘点 OIR Knowledge 表、Collection、本地文件和持久化 JSON
- **THEN** 输出仅包含类型、Schema 版本、数量、聚合 Hash 和计划动作，不包含正文

#### Scenario: 遇到未知 JSON 结构
- **WHEN** 清理器发现无法证明为 Knowledge Context 正文的结构
- **THEN** 清理立即停止且不执行模糊删除

### Requirement: 知识切换前必须规划迁移映射
系统 SHALL 把 OIR Knowledge 副本处置定义为“不迁移、只清理”，并记录 OIR 269 Chunk 与
`knowledge_sys` 270 Chunk 的已知差异；MUST NOT 创建从 OIR 到 `knowledge_sys` 的数据或
向量迁移映射。

#### Scenario: 对账现有知识副本
- **WHEN** 清理前比较 OIR 与 `knowledge_sys` 的 Knowledge 计数
- **THEN** 269/270 差异被记录为解析差异，`knowledge_sys` 结果保持唯一 Canonical Data

#### Scenario: 清理完成
- **WHEN** Manifest、Dry Run、执行和 Memory 不变性报告均通过
- **THEN** OIR 不再保存 Knowledge 表、专属 Collection、本地向量文件或持久化正文

### Requirement: 知识过渡不阻塞 mem0 记忆闭环
系统 SHALL 将 Knowledge 外置与 OIR Memory 生命周期解耦；Memory 使用向量数据库不构成
OIR 拥有 Knowledge 的理由。

#### Scenario: Knowledge Provider 未配置
- **WHEN** OIR 启动且没有 Knowledge Provider
- **THEN** Memory Recall、Formation、Create、Update 和 Delete 仍按显式 Memory 配置工作

#### Scenario: Memory 配置缺失
- **WHEN** Memory 已启用但必要 `MEMORY_*` 配置缺失
- **THEN** OIR 启动失败，不回退 Knowledge 配置或静默创建新 Collection
