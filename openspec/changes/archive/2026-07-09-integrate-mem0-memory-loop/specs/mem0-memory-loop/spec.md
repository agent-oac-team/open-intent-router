## ADDED Requirements

### Requirement: 可显式配置 mem0 记忆策略
系统 SHALL 允许运维或开发者通过文档化的 Settings 字段启用真实 mem0 记忆策略，而不是只能依赖原始 JSON 配置。

#### Scenario: 使用 Milvus Lite 配置 mem0 记忆 collection
- **WHEN** `MEMORY_STRATEGY_PROVIDER=mem0` 且提供 Milvus 记忆配置
- **THEN** 系统使用 `milvus` vector store provider、Milvus Lite URI 和 `oir_memory_vectors` collection 构造 mem0 `Memory.from_config()` 配置

#### Scenario: 使用阿里 embedding 配置
- **WHEN** 启用 mem0 记忆策略
- **THEN** 系统默认沿用 IRS 兼容的阿里/OpenAI-compatible embedding base URL、model、API key 和 embedding 维度，除非部署方显式覆盖

#### Scenario: JSON override 仍可使用
- **WHEN** 配置了 `MEM0_CONFIG_JSON`
- **THEN** 系统将其作为高级覆盖配置使用，并在可行时继续在 debug metadata 中暴露 provider 和 collection 信息

### Requirement: mem0 history 写入 PostgreSQL
系统 SHALL 将 mem0 记忆操作历史和外部 ID 映射持久化到 PostgreSQL。

#### Scenario: 成功写入记忆时记录 history
- **WHEN** 一条已接受记忆被写入 mem0
- **THEN** 系统在 PostgreSQL history 或 ledger 存储中记录操作类型、OIR memory ID、mem0 memory ID、user、tenant、subject、scope、collection、embedding model、status 和 timestamp

#### Scenario: SDK 缺少 PostgreSQL history backend
- **WHEN** 当前 mem0 Python SDK 只暴露 SQLite `history_db_path`
- **THEN** 系统 MUST 仍然在 OIR PostgreSQL 存储中持久化 canonical mem0 history，并且 MUST NOT 将 SDK SQLite history 视为 canonical history 来源

### Requirement: mem0 写入经过 OIR 治理
系统 SHALL 先通过 OIR policy 判断记忆候选，再把已接受的记忆发送给 mem0。

#### Scenario: 接受的候选写入 mem0 和 OIR metadata
- **WHEN** 低风险记忆候选通过 OIR confidence、sensitivity、scope、tenant 和 visibility policy
- **THEN** 系统将记忆内容和治理 metadata 写入 mem0，并在 OIR 存储中持久化 accepted memory item 和 memory event

#### Scenario: 拒绝的候选不写入 mem0
- **WHEN** 记忆候选被 OIR policy 拒绝
- **THEN** 系统 MUST NOT 对该候选调用 mem0 add，并 SHALL 返回带 reason 的 rejected write decision

### Requirement: mem0 召回进入 memory_context
系统 SHALL 使用 mem0 search 结果组装现有 `memory_context` 契约。

#### Scenario: 搜索返回治理后的记忆项
- **WHEN** Agent 声明 `context.memory.mode=prefetch`
- **THEN** 系统使用请求 query、user identity、scopes、subject、tenant 和 agent filters 搜索 mem0，并将返回记忆映射到 `memory_context.items`

#### Scenario: 搜索保持现有 context 契约
- **WHEN** mem0 返回匹配记忆
- **THEN** invocation input 仍包含与现有 Agent context 契约兼容的 `memory_context.summary`、`memory_context.items`、`memory_context.status`、`memory_context.truncated` 和 debug metadata

### Requirement: mem0 失败必须可见且区分环境
系统 SHALL 区分本地 fallback 和 fail-closed mem0 失败。

#### Scenario: 本地 fallback 记录 degraded 状态
- **WHEN** mem0 在 local fallback 模式下不可用
- **THEN** 系统可以使用 repository memory fallback，但 SHALL 在 debug metadata 或日志中暴露 degraded 状态和错误详情

#### Scenario: 生产失败不能静默成功
- **WHEN** mem0 不可用且 fail-closed 模式生效
- **THEN** 系统 MUST 返回结构化 memory error 或 rejected write decision，并且 MUST NOT 把操作报告为已由 mem0 成功支持

### Requirement: mem0 ID 与 OIR ID 可追踪
系统 SHALL 保留 OIR memory ID 与 mem0 memory ID 之间的可追踪关系。

#### Scenario: mem0 返回自己的 memory ID
- **WHEN** mem0 add 或 search 返回的 ID 与 OIR `memory_id` 不同
- **THEN** 系统在 memory metadata 中保存或暴露 mem0 ID，但不替换 OIR canonical memory ID

#### Scenario: 清理时删除外部记忆记录
- **WHEN** OIR 过期或删除带有已知 mem0 ID 的记忆项
- **THEN** 系统尝试删除对应 mem0 记录，并将任何外部清理失败记录为可观测 metadata 或 event

### Requirement: 真实基础设施 smoke test 有文档
系统 SHALL 提供可选 smoke 路径，用于验证真实 PostgreSQL、Milvus Lite 和阿里 embedding 下的完整 mem0 记忆闭环。

#### Scenario: Smoke test 证明写入和召回闭环
- **WHEN** 开发者在 PostgreSQL、mem0、Milvus Lite 和阿里 embedding 凭证可用时运行文档化 smoke 路径
- **THEN** 测试验证 memory write、mem0 indexing、PostgreSQL history persistence、OIR metadata/event persistence、mem0 recall 和 `memory_context` assembly

#### Scenario: 缺少 Milvus Lite 或 embedding 时清晰失败
- **WHEN** smoke 路径在 Milvus Lite URI 不可用或阿里 embedding 配置不可用时运行
- **THEN** 失败消息标识 vector store 或 embedding 配置问题，而不是通过 repository fallback 造成假成功
