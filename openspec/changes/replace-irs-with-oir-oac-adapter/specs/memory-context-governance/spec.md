## MODIFIED Requirements

### Requirement: 可显式配置 mem0 记忆策略
系统 SHALL 只通过文档化的 `MEMORY_*` Settings 字段配置真实 mem0 记忆策略，
MUST NOT 使用通用 JSON override、Router LLM、通用 Embedding 或 Knowledge 配置补齐
Memory 基础设施。

#### Scenario: 使用显式 Milvus 与 Embedding 配置
- **WHEN** `MEMORY_STRATEGY_PROVIDER=mem0` 且 Memory 已启用
- **THEN** 系统只用 `MEMORY_MILVUS_*` 与 `MEMORY_EMBEDDING_*` 构造 mem0 配置

#### Scenario: mem0 使用 LLM
- **WHEN** 部署方需要为 mem0 配置 LLM
- **THEN** 系统只接受显式 `MEMORY_MEM0_LLM_*`，不复用 Router LLM

#### Scenario: 提交旧高级 override
- **WHEN** 部署仍提供已退役的 `MEM0_CONFIG_JSON`
- **THEN** 系统不读取该值，也不能据此改变 Memory Store、Collection 或 Embedding

### Requirement: mem0 provides memory strategy behind an adapter
系统 SHALL 在 Memory Adapter 边界后使用 mem0 作为默认记忆策略引擎，自动 Memory
Formation 只能消费已完成且所有权已验证的 Canonical Conversation Turn。外部 Knowledge
Context、孤立消息、不受信 Host 载荷和部分 Agent/Plan 结果 MUST NOT 成为 Formation 证据。

#### Scenario: 从已完成 Canonical Turn 形成 Memory
- **WHEN** 已完成的 Canonical Turn 通过 Transactional Outbox 发布
- **THEN** 系统只把经过治理且合格的 Turn Capsule 发送给 Memory Adapter

#### Scenario: Turn 使用外部 Knowledge Context
- **WHEN** Agent Result 或 Invocation 使用过外部 Knowledge 正文
- **THEN** 正文从 Formation 证据中移除，不能复制进长期 Memory

#### Scenario: 通过策略层检索 Memory
- **WHEN** 系统为 Route 或 Invocation 召回 Memory
- **THEN** 系统使用 user、subject、scope、metadata filters、query 和 limit 调用 Memory Adapter

#### Scenario: Adapter 结果由 OIR 治理
- **WHEN** mem0 返回 Memory 候选
- **THEN** OIR 在暴露候选前应用租户开关、Agent Scope、权限、TTL、脱敏和预算规则

## ADDED Requirements

### Requirement: Memory 只使用显式 MEMORY 配置
系统 SHALL 使用完整 `MEMORY_*` 命名空间配置 Memory Store、Embedding 与 Collection，
MUST NOT 回退 `KNOWLEDGE_*` 配置、Knowledge Transition 元数据或隐式默认 Collection。

#### Scenario: Memory 配置完整
- **WHEN** Memory 启用且所有必要显式配置存在
- **THEN** OIR 使用既有 Memory Collection 和 Embedding 配置启动

#### Scenario: Memory 配置缺失
- **WHEN** Memory 启用但必要显式配置缺失
- **THEN** OIR 启动失败，不静默换用 Knowledge 配置或创建新 Collection

### Requirement: Memory 解耦必须证明数据不变
系统 SHALL 在删除 Knowledge 配置回退前后对账 Memory Collection、Embedding 配置、数量、
Recall、Create、Update 和 Delete，且 MUST NOT 重建、重新 Embedding 或迁移现有 Memory。

#### Scenario: 基线验证失败
- **WHEN** 显式 Memory 配置无法复现当前 Collection、数量或行为
- **THEN** 迁移停止，不删除 Knowledge fallback

#### Scenario: 解耦验证通过
- **WHEN** 前后配置、数量和 CRUD/Recall 结果一致
- **THEN** 系统可删除 fallback、transition metadata 和 Knowledge/Memory 交叉校验

### Requirement: Shadow Memory Formation 必须隔离于主召回数据域
系统 SHALL 在 State Rehearsal 中将 Memory 写入独立测试 database/schema 与 Collection，
Decision Shadow MUST NOT 创建可被主链路召回的 Memory。

#### Scenario: Decision Shadow 产生可形成 Turn
- **WHEN** OIR Shadow 结果包含潜在记忆候选
- **THEN** 系统只记录无副作用决策证据，不写入主 Memory Store

### Requirement: Memory Formation 与 Recall 必须可独立 mode-off
系统 SHALL 提供 Formation、Recall 和 Worker 独立关闭开关，并在关闭时保留可观测状态。

#### Scenario: Formation mode-off
- **WHEN** 运维关闭自动 Formation
- **THEN** Canonical Turn 仍正常完成，但不新增或修改 Memory

#### Scenario: Recall mode-off
- **WHEN** 运维关闭 Recall
- **THEN** Route 与 Invocation 继续运行，且不回退到 Knowledge 检索
