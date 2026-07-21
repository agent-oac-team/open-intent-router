## ADDED Requirements

### Requirement: route-and-invoke 必须可靠收口 Canonical Turn
系统 SHALL 为每个通过可信身份验证的 `route-and-invoke` 请求幂等创建 Canonical Turn，在 Agent 执行前原子关联预创建 Run，并在终态 Result 到达后完成 Turn；成功 HTTP 响应 MUST NOT 对应一个仍停留在 `pending/running` 的 Turn。

#### Scenario: Agent 调用成功
- **WHEN** 路由选择可调用 Agent 且 Agent 返回有效 completed Result
- **THEN** 系统持久化 Run、Result、用户可见最终语义响应和 completed Turn，并返回原有兼容响应

#### Scenario: Agent 调用失败或输出无效
- **WHEN** Agent 调用失败、超时或输出不符合 schema
- **THEN** 系统以相应失败语义终态收口 Turn 并保留结构化错误，MUST NOT 让 Turn 无限停留在非终态

#### Scenario: 相同 request ID 重试
- **WHEN** 所有权和输入一致的 `route-and-invoke` 请求使用相同 request ID 重试
- **THEN** 系统复用已有 Turn/Run/Result 终态或返回幂等结果，不创建第二个逻辑执行和第二个形成事件

### Requirement: Run 启动和终态收口必须满足事务一致性
系统 SHALL 在 Agent 外部调用前以单一事务写入 Run 并把 Turn 更新为 `running`，在 Agent 返回后以另一个单一事务更新终态 Run、插入 Result、完成 Turn 并插入 Transactional Outbox；外部 Agent 调用 MUST NOT 被包含在长数据库事务中。

#### Scenario: Run 启动事务失败
- **WHEN** Run 插入或 Turn 关联任一步骤失败
- **THEN** 启动事务整体回滚且系统不调用外部 Agent

#### Scenario: 终态事务失败
- **WHEN** Run、Result、Turn 或 Outbox 的任一终态写入失败
- **THEN** 终态事务整体回滚，不出现 Result 已成功但 Turn 仍 pending 的部分状态

#### Scenario: 终态事务提交成功
- **WHEN** Run/Result 所有权、Turn 版本和引用约束均通过
- **THEN** completed Turn 与唯一 `turn.completed` Outbox 事件同时可见

### Requirement: completed Turn Outbox 是会话自动形成的唯一权威入口
系统 SHALL 仅从 completed、所有权已验证且包含用户输入与最终语义响应的 Canonical Turn Outbox 事件创建会话 Formation Turn/Job；对已有 Canonical Turn 的请求 MUST 禁用 legacy direct turn capture，结构化 Run/Result task projection 不得替代或重复会话偏好形成。

#### Scenario: Outbox 首次消费
- **WHEN** consumer 首次处理有效 `turn.completed` 事件
- **THEN** 系统幂等创建对应 Formation Turn，并按窗口或 idle 规则创建 Formation Job

#### Scenario: Outbox 重复投递
- **WHEN** 同一 completed Turn 的事件被至少一次投递机制重复消费
- **THEN** 系统复用同一 Formation Turn/Job 或返回已处理状态，不生成重复 Memory Revision 或 Provider Write

#### Scenario: legacy capture 同时可用
- **WHEN** Canonical route-and-invoke 请求已进入 Turn Outbox 链路
- **THEN** InvocationService MUST NOT 再通过 legacy direct capture 为同一 request/run 创建第二个 Formation Turn

### Requirement: enforced 模式必须完成记忆持久化与召回闭环
系统 SHALL 在 `memory_formation_mode=enforced` 时把有效候选依次通过 schema、policy、lifecycle、revision、index outbox 和 mem0 adapter，并仅在 canonical ledger 与索引均达到可用状态后把该形成结果标记为 `persisted`。

#### Scenario: 用户在业务请求中表达低风险偏好
- **WHEN** 用户输入“明天要拜访一位关注稳健理财的客户，帮我做访前准备。我喜欢吃猪肉。”且形成模型输出有证据支持的低风险 food preference candidate
- **THEN** 系统完成 user preference Memory Item、Revision 和 mem0 index，并可在后续同一用户的受允许 scope recall 中命中该偏好

#### Scenario: 模型未产生候选
- **WHEN** Formation Job 成功执行但模型判断没有可持久化候选
- **THEN** 系统记录 `completed_no_candidate` 终态并保留 job/turn 关联，MUST NOT 将其误报为未触发

#### Scenario: 模型返回无效响应后重试成功
- **WHEN** Formation Model 首次返回无效严格 JSON，后续重试返回有效候选
- **THEN** 同一 Job 记录尝试和最后错误历史后完成一次持久化，不重复写入记忆

#### Scenario: Indexing 暂时失败
- **WHEN** canonical Memory Item/Revision 已提交但 mem0 index operation 暂时失败
- **THEN** 形成状态显示 `index_pending/retry`，worker 可恢复索引且 MUST NOT 把未索引记录报告为完整 persisted

### Requirement: 滞留 Turn 必须可对账且受控修复
系统 SHALL 提供可重复、默认 dry-run 的对账能力，识别有所有权一致终态 Result 但 Turn 未完成或缺少 Outbox 的记录，并依据原始 Formation 抑制/策略证据决定修复动作。

#### Scenario: Formation 当时开启的可修复 Turn
- **WHEN** pending Turn 存在唯一的同所有者 completed Run/Result，且没有 Formation 抑制证据
- **THEN** 操作员可幂等完成 Turn 并补建 Outbox，使其重新进入正常 Formation 链路

#### Scenario: Formation 当时关闭或请求明确禁止记忆
- **WHEN** 历史 Run/Result 表明 `formation_suppressed` 或请求具有 temporary/private 策略
- **THEN** 对账只完成 Turn 和 skipped 审计，MUST NOT 因当前配置已开启而追溯写入长期记忆

#### Scenario: 关联不唯一或所有权冲突
- **WHEN** pending Turn 无法唯一关联 Run/Result或 tenant/user/request 不一致
- **THEN** 对账将其报告为人工处理项，不修改记录且不暴露跨所有者正文

### Requirement: 完整链路必须可通过关联标识验证
系统 SHALL 允许受授权调试调用从 request ID 追踪 Canonical Turn、Run、Result、Outbox、Formation Turn/Job、Lifecycle Decision、Memory Item/Revision、Index Operation 和 Recall 使用记录，并仅返回安全错误码和有界预览。

#### Scenario: 形成成功
- **WHEN** 调试端按成功请求 ID 查询
- **THEN** 响应提供从 request 到 persisted/indexed memory 的完整关联和各阶段时间戳

#### Scenario: 任一阶段失败或跳过
- **WHEN** 链路停在收口、Outbox、Formation、Policy、Lifecycle 或 Index 阶段
- **THEN** 响应显示最后确定阶段、可重试性和安全 reason/error code，不把未知状态显示为成功或未触发
