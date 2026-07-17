## ADDED Requirements

### Requirement: 每个被接受的语义请求只创建一个 Canonical Turn
系统 SHALL 使用可信 `tenant_id/user_id/request_id` 幂等创建 Canonical Conversation Turn，并在 Turn 中保留 session、source、受控输入、路由决策、Run/Result/Plan 引用、最终语义响应与版本状态。

#### Scenario: 首次接受 Route 请求
- **WHEN** 一个通过身份验证的新 `request_id` 进入 Route 应用服务
- **THEN** 系统创建一个绑定 tenant/user/session 所有权的 Canonical Turn

#### Scenario: 同一请求幂等重试
- **WHEN** 相同 tenant/user/request ID 的请求重试且输入身份不冲突
- **THEN** 系统返回原逻辑 Turn 或其当前结果，不创建第二个 Turn

#### Scenario: 幂等键身份冲突
- **WHEN** 已有 request ID 被不同 tenant/user 或冲突输入重用
- **THEN** 系统拒绝请求并记录幂等冲突，不改变原 Turn

### Requirement: Route-only 路径在无伪 Run 的情况下完成 Turn
系统 SHALL 对 `reply/clarify/unsupported/silent` 等无 Agent 执行路径写入最终语义响应并直接完成 Turn，MUST NOT 为了满足轮次形状而伪造 Agent Run。

#### Scenario: Router 直接回答
- **WHEN** Route 决策为 `reply` 且存在最终 assistant message
- **THEN** Turn 以用户输入和该语义响应完成，Run 引用为空

#### Scenario: Router 请求澄清
- **WHEN** Route 决策为 `clarify`
- **THEN** 澄清问句成为当前 Turn 的最终语义响应，后续用户回答创建新 Turn

### Requirement: 包含 Agent 执行的 Turn 只在可信 Result 收口后完成
系统 SHALL 将需要 Agent 执行的 Turn 与已预创建 Run 关联，并在所有权一致的最终 Result 收口后才将 Turn 转为终态。

#### Scenario: Agent Run 仍在执行
- **WHEN** Turn 的关联 Run 为 pending/running/blocked 且没有最终 Result
- **THEN** Turn 保持非终态且不触发最终 Memory Formation

#### Scenario: Agent 返回最终成功结果
- **WHEN** 有效 Run 提交所有权一致的 completed Result
- **THEN** 系统关联 Result 与用户可见响应并完成对应 Turn

#### Scenario: 迟到结果尝试重开 Turn
- **WHEN** 一个重复或迟到 Event 指向已封闭 Turn
- **THEN** 系统返回幂等结果或终态冲突，MUST NOT 重开 Turn 或生成第二个最终 Result

### Requirement: Turn 所有权与状态迁移必须可验证
系统 SHALL 在读取或更新 Turn 时验证 tenant/user/session/request 所有权，并使用乐观版本或条件更新防止并发静默覆盖。

#### Scenario: 跨用户更新 Turn
- **WHEN** 命令的 tenant/user 与 Turn 所有者不一致
- **THEN** 系统拒绝操作且不暴露 Turn 正文或存在性细节

#### Scenario: 并发终态更新
- **WHEN** 两个回调使用同一旧 state version 尝试封闭 Turn
- **THEN** 只有一个更新成功，另一个收到幂等或版本冲突结果

### Requirement: Turn 完成与派生事件在同一事务收口
系统 SHALL 在同一 PostgreSQL 事务中持久化 Turn 终态、关联 Run/Result/Plan 更新和 Transactional Outbox 事件，不得在主事务中同步调用 Memory Provider。

#### Scenario: 主事务成功提交
- **WHEN** Run/Result/Plan/Turn 收口的所有不变量均通过
- **THEN** 系统一次提交所有 canonical 更新和对应 Outbox 事件

#### Scenario: 主事务中任一更新失败
- **WHEN** Result、Plan、Turn 或 Outbox 的任一必要写入失败
- **THEN** 整个事务回滚，不出现 Result 已成功但 Turn/Plan 未更新的部分状态

### Requirement: 只有可信完成 Turn 可以进入 Memory Formation
系统 SHALL 只向 Memory Formation 发布已完成、所有权可验证、具有用户输入与最终语义响应的 Canonical Turn，且每个 Turn 最多发布一次可消费形成事件。

#### Scenario: 完整 Turn 提交后异步形成
- **WHEN** Canonical Turn 主事务成功提交
- **THEN** Formation Worker 可通过 Outbox 获得完整 Turn Capsule，并按治理策略处理记忆

#### Scenario: 非终态或孤儿 Turn
- **WHEN** Turn 仍为 pending/running/blocked、所有权不完整或缺少最终响应
- **THEN** 系统不得将其作为自动 Memory Formation 输入

### Requirement: Canonical Turn 不承担 Host 展示消息兼容职责
系统 SHALL 将 Canonical Turn 定义为语义运行事实，MUST NOT 要求 Host UI 消息行、Provider 会话 ID、OAC 页面状态或历史消息副本成为 Turn 一等字段。

#### Scenario: Host 有自己的展示消息
- **WHEN** Host 在自身读模型中保存或不保存消息
- **THEN** Canonical Turn 仍只根据可信语义请求与最终结果维护自身不变量
