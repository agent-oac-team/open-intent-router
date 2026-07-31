## MODIFIED Requirements

### Requirement: Knowledge search is a general governed API
系统 SHALL 通过 Provider-neutral 的异步 `KnowledgeProvider.retrieve(request) -> result`
端口为 Agent Context 检索外部 Knowledge；OIR Core MUST NOT 提供 Knowledge Search HTTP
API 或镜像后端的 Search、Grouped Search、Read、Assets、Chunks、Admin 操作。

#### Scenario: Agent Invocation 请求知识
- **WHEN** Agent 的有效 Context Policy 要求预取 Knowledge
- **THEN** OIR 使用受信 Principal、Query、Purpose、Consumer、逻辑 Source Scope 和预算调用 Knowledge Provider

#### Scenario: 路由需要证据
- **WHEN** Route Decision 需要固定问、意图提示或证据
- **THEN** Router 只调用 Evidence Provider，不把 Knowledge Provider 用作 Route Override 或候选 Agent 来源

#### Scenario: Provider 未配置
- **WHEN** 部署未解析出 Knowledge Provider
- **THEN** OIR 路由与编排仍可运行，Agent Invocation 按 Knowledge Requirement 处理结构化不可用状态

### Requirement: Knowledge source policy is authoritative
系统 SHALL 将 `source_ids` 解释为稳定逻辑 Source Key、将 `source_tags` 解释为内容分类；
OIR 不把二者视为授权凭据，最终 Knowledge ACL 与物理 Asset/Group/Index 解析归知识所有者。

#### Scenario: Agent 声明 Source Key
- **WHEN** Agent Definition 声明 `context.knowledge.source_ids`
- **THEN** OIR 只传递逻辑范围，不解析数据库主键、Collection 或文件路径

#### Scenario: Provider 拒绝访问
- **WHEN** 受信 Principal 无权访问请求范围
- **THEN** Provider 返回 denied，OIR 不通过自报 tag 或本地知识副本绕过拒绝

### Requirement: Knowledge context is explicit and structured
系统 SHALL 只在 Context Policy 明确启用时组装 `knowledge_context`，并保持结果
Provider-neutral。每个 Item 必须有 `item_id`，每次结果必须有 `trace_id`，Citation 最小只
要求逻辑 `source_id`。

#### Scenario: Knowledge prefetch succeeds
- **WHEN** Provider 返回治理后可用的 Knowledge Items
- **THEN** Agent 收到结构化 items、citations、status 与 truncation，而 Locator、Source Version 和 Content Hash 不作为必填字段

#### Scenario: Knowledge prefetch returns no hits
- **WHEN** Provider 成功但治理后没有可用 Item
- **THEN** optional Invocation 收到 `status=empty`，required Invocation 以 `knowledge_not_found` 阻止 Agent 调用

#### Scenario: Knowledge context is truncated
- **WHEN**结果超过 `max_items`、`max_chars` 或 Context Budget
- **THEN** OIR 投影有界上下文并设置 `truncated=true`

### Requirement: Controlled retrieval uses fixed templates
系统 SHALL 只允许受治理工作流按固定模板执行 controlled retrieval；required 模式还必须
消费 OIR 签发、短时有效并绑定 tenant、principal、Agent、Source Scope 与 trace 的
Knowledge Context Handle。

#### Scenario: Controlled optional 未显式调用
- **WHEN** Agent 使用 `controlled_retrieval + optional` 且工作流未执行检索节点
- **THEN** OIR 不自动预取 Knowledge，Invocation 可继续

#### Scenario: Controlled required 缺少可信 Handle
- **WHEN** required Invocation 只携带调用方自报的 `knowledge_context` 或无效 Handle
- **THEN** OIR 拒绝把它视为成功检索并阻止 Agent 调用

#### Scenario: Controlled required 使用有效 Handle
- **WHEN** Handle 的 tenant、principal、Agent、Source Scope、trace、有效期和消费状态均匹配
- **THEN** OIR 允许把对应瞬时 Knowledge Context 投影给 Agent

### Requirement: Knowledge retrieval uses PostgreSQL and Milvus domains
系统 SHALL NOT 在 OIR 中持久化 Knowledge metadata、Chunk、检索日志或向量；知识所有者
可使用 PostgreSQL、Milvus 或其他实现，但这些存储不进入 OIR Core 契约。

#### Scenario: OIR Provider Adapter 返回结果
- **WHEN** 外部知识所有者完成检索
- **THEN** OIR 只接收 Provider Result，不直接访问其 SQL、Milvus、Asset 或 Chunk Repository

#### Scenario: OIR 持久化运行事实
- **WHEN** Agent Invocation 使用了 Knowledge
- **THEN** Run、Event、Trace 和日志只保存 Citation、Provider Trace、Item ID、数量、状态与错误码，不保存正文

### Requirement: Knowledge retrieval degrades safely
系统 SHALL 按 `optional|required` Knowledge Requirement 处理 Provider 缺失、拒绝、超时、
故障和空结果，默认 Requirement 为 optional。

#### Scenario: Optional Provider 失败
- **WHEN** optional 检索返回 denied、timeout、unavailable 或 error
- **THEN** OIR 携带结构化状态继续调用 Agent

#### Scenario: Required Provider 失败
- **WHEN** required 检索未配置 Provider或返回 denied、timeout、unavailable 或 error
- **THEN** OIR 不调用 Agent并返回稳定错误 `knowledge_unavailable`

#### Scenario: Provider Deadline 到期
- **WHEN** 检索超过默认 12 秒、可配置的 OIR 总 Deadline
- **THEN** OIR 取消等待、不自动重试，并将结果映射为 timeout

## ADDED Requirements

### Requirement: Knowledge Provider 不得影响 Route Decision
系统 SHALL 将 Knowledge Provider 与 Evidence Provider 建模为独立端口；Knowledge Result
MUST NOT 包含 Route Override、候选 Agent 或目标 Agent。

#### Scenario: Provider 实现尝试返回路由字段
- **WHEN** Knowledge Provider Adapter 响应包含 Route Override 或 Agent 选择字段
- **THEN** 契约校验拒绝该响应，Router 不消费这些字段

### Requirement: OIR 不得跨请求保存 Knowledge 正文
系统 SHALL 只允许单请求内对等价检索去重，不建立跨请求 Knowledge Cache；知识正文仅在
当前 Invocation 内存载荷中存在。

#### Scenario: 下一请求发起同一查询
- **WHEN** 相同 Principal 和 Query 在新请求中再次出现
- **THEN** OIR 重新调用 Provider，不复用上次正文

#### Scenario: 异步执行需要传递正文
- **WHEN** Agent Invocation 无法在同一同步调用栈完成
- **THEN** 正文只进入加密、短 TTL 临时载荷，并在任务终态清除

### Requirement: Provider Adapter 使用有界 Deadline 与 Circuit
系统 SHALL 默认使用 12 秒 OIR 总 Deadline、零自动重试，并在 30 秒内连续 5 次 timeout、
unavailable 或 5xx 后熔断 30 秒，再允许一次 half-open 探测；所有参数可由环境变量覆盖。

#### Scenario: 正常空结果或权限拒绝
- **WHEN** Provider 返回 empty、denied、JWT 4xx 或业务校验错误
- **THEN** 该结果不计入 Circuit 连续故障

#### Scenario: Circuit 已打开
- **WHEN** 新检索在 30 秒打开窗口内到达
- **THEN** OIR 不调用 Provider，将结果映射为 unavailable 并按 Requirement 处理
