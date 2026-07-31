## MODIFIED Requirements

### Requirement: Agent Definition declares context needs
系统 SHALL 继续允许 Agent Definition 在通用 `context` 中声明 Memory 与 Knowledge，
Knowledge 声明只包含 mode、requirement、逻辑 source IDs/tags 和预算，不包含 Provider
名称、URL、IRS、`knowledge_sys`、数据库或索引标识。

#### Scenario: Agent 声明 Knowledge prefetch
- **WHEN** Agent 使用 `context.knowledge.mode=prefetch`
- **THEN** OIR 按部署与租户策略解析 Provider，并在 Invocation 前执行 Knowledge Requirement

#### Scenario: Agent 未声明 Knowledge
- **WHEN** Agent 省略 `context.knowledge` 或设置 `mode=disabled`
- **THEN** OIR 不检索 Knowledge，Requirement 按默认 optional 处理

### Requirement: Agent context config remains generic
系统 SHALL 使同一 Agent Context Schema 可用于任意 Knowledge Provider 实现；Agent
Definition MUST NOT 选择后端或暴露物理 Asset、Group、Collection 和文件路径。

#### Scenario: 更换 Knowledge Provider 实现
- **WHEN** 部署策略从一个 Provider 切换到另一个或 Composite Provider
- **THEN** Agent Definition 和 Invocation 契约无需修改

### Requirement: Invoked Agents receive deterministic context fields
系统 SHALL 保持稳定的 `memory_context` 与 `knowledge_context` Invocation 字段；Agent
不需要知道 Provider、HTTP API、数据库、向量索引或 Context Handle 机制。

#### Scenario: Invocation 收到 Knowledge Context
- **WHEN** Knowledge 检索成功或 optional 路径降级
- **THEN** Agent 收到有界的 items、citations、source IDs、status 与 truncation 字段

#### Scenario: Invocation 被持久化
- **WHEN** Run、Event、Trace 或日志记录 Agent 调用
- **THEN** 持久化投影移除 Knowledge 正文，只保留引用与结构化状态

### Requirement: Context modes are controlled by platform policy
系统 SHALL 支持 `disabled`、`prefetch` 和 `controlled_retrieval`，并将 Knowledge
Requirement 限定为 `optional|required`，默认 optional。

#### Scenario: Disabled 配置 required
- **WHEN** Agent Definition 组合 `mode=disabled` 与 `requirement=required`
- **THEN** 配置校验失败

#### Scenario: Prefetch required
- **WHEN** required Agent 使用 prefetch
- **THEN** OIR 只有在 Provider 成功返回至少一条治理后可用 Item 时才调用 Agent

### Requirement: Context prefetch integrates with Context Pack budget
系统 SHALL 将 Provider Items 通过 Context Pack 预算和消费者投影后再交付 Agent，且预算
处理不得把外部 Knowledge Context 变为 Memory Formation 证据。

#### Scenario: Knowledge 超出预算
- **WHEN** Provider Items 超出 Context Pack 限制
- **THEN** OIR 截断或删除低优先级项、设置 truncated，并仅持久化引用
