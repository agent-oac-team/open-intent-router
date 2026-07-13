## ADDED Requirements

### Requirement: 所有自动记忆来源使用统一形成流水线
系统 SHALL 将普通对话窗口、空闲会话、结构化任务事件、后台治理和管理命令归一化为同一 `MemoryFormationPipeline` 的 job、candidate、policy decision 和 lifecycle operation，而不是为不同触发器维护独立写入逻辑。

#### Scenario: 不同触发器进入同一决策链路
- **WHEN** turn window、idle 或 structured event 产生记忆形成请求
- **THEN** 系统使用相同 candidate schema、policy version、idempotency、lifecycle service 和 formation trace 处理请求

#### Scenario: 触发只决定评估时机
- **WHEN** 任一形成触发器被执行
- **THEN** 触发器 MUST NOT 绕过 policy 直接调用 mem0 add、update 或 delete

### Requirement: 完整对话轮次持久化为有界 Turn Capsule
系统 SHALL 在 Agent run/result 持久化后，为同一请求记录包含 user、assistant/Agent response 和必要 canonical references 的有界 Turn Capsule。

#### Scenario: 成功 route-and-invoke 形成完整 turn
- **WHEN** `route-and-invoke` 完成 Agent 调用并持久化 Agent Result
- **THEN** 系统记录 request/session/run/user/tenant/agent 标识、bounded user/assistant text、result/plan/artifact refs、used memory IDs 和 completed timestamp

#### Scenario: 无界结果不进入 capsule
- **WHEN** Agent Result、Artifact 或 memory context 包含超出形成限制的正文或结构化值
- **THEN** capsule 只保存 bounded summary、stable refs 或 hashes，并 MUST NOT 复制完整 Result、Artifact 正文或完整 memory context

#### Scenario: 失败调用不证明用户事实
- **WHEN** Agent invocation 失败或输出无效
- **THEN** 系统可以记录可调试 capsule 状态，但 MUST NOT 仅以错误消息或 Assistant 输出为证据生成用户长期事实

### Requirement: 普通对话按五轮窗口触发形成
系统 SHALL 默认在同一 tenant/user/session 累计 5 个尚未形成的完整 turn 后创建异步 formation job，窗口大小 SHALL 可配置。

#### Scenario: 第五轮创建窗口 job
- **WHEN** 同一 session 的第 5 个 pending complete turn 被持久化
- **THEN** 系统冻结这 5 个 turn 的范围并创建 trigger=`turn_window` 的 formation job

#### Scenario: 少于五轮不提前创建窗口 job
- **WHEN** session 有 1 到 4 个 pending complete turns 且尚未达到 idle deadline
- **THEN** 系统不因 turn count 创建 formation job

#### Scenario: job 运行期间新 turn 进入下一窗口
- **WHEN** 一个 frozen turn range 的 formation job 正在执行且新 turn 到达
- **THEN** 新 turn 不并入已冻结 range，并作为下一个 pending range 的起点

### Requirement: 普通对话在空闲三十秒时触发形成
系统 SHALL 默认在存在 pending turns 且 30 秒没有新 complete turn 时创建 idle formation job，并 SHALL 使用持久化 deadline/sweeper 而不是只依赖进程内 timer。

#### Scenario: 未满五轮后空闲
- **WHEN** session 有 1 到 4 个 pending turns 且 idle deadline 到期
- **THEN** sweeper 创建 trigger=`idle` 的 formation job，覆盖全部尚未 claimed 的 pending range

#### Scenario: 新 turn 重置 idle deadline
- **WHEN** idle deadline 到期前同一 session 又完成一个 turn
- **THEN** 系统基于最新 complete turn 重置 deadline

#### Scenario: Host 不发送 session close
- **WHEN** Host 从未发送可靠 session close 事件
- **THEN** turn window 和 idle sweeper 仍能完成所有 pending turn 的形成评估

#### Scenario: 进程重启后恢复 idle trigger
- **WHEN** 服务在 deadline 前重启且数据库中仍有 pending turns
- **THEN** sweeper 从持久化 deadline/watermark 恢复并创建应执行的 job

### Requirement: Formation job 对竞态和重试幂等
系统 SHALL 使用 tenant、session、frozen turn range 和 policy version 构造稳定 idempotency key，并使用 claim/lease/watermark 防止并发 worker 重复形成。

#### Scenario: Window 和 idle 同时触发
- **WHEN** 五轮阈值和 idle sweeper 对同一 pending range 并发创建 job
- **THEN** 唯一约束使该 range 只有一个可执行 formation job

#### Scenario: Worker lease 超时
- **WHEN** worker claim job 后在完成前崩溃且 lease 到期
- **THEN** 另一 worker 可以重新 claim 同一 job，且 candidate/lifecycle idempotency 防止重复 memory 或 revision

#### Scenario: 失败不推进成功 watermark
- **WHEN** formation model、policy persistence 或 lifecycle operation 失败
- **THEN** 系统记录 attempt/error 并 MUST NOT 把该 turn range 标记为 successfully formed

### Requirement: 普通对话由 OIR 形成模型输出严格候选
系统 SHALL 使用 OIR `ConversationFormationModel` 从冻结 turn range 生成严格结构化候选，并 SHALL 在进入 policy 前完成 schema 验证。

#### Scenario: 模型输出有效候选
- **WHEN** 形成模型识别到可长期复用的偏好、事实、任务引用或 summary
- **THEN** 每个候选包含 proposed operation、scope、content、subject hint、memory key hint、confidence、importance、sensitivity、evidence refs 和 reason

#### Scenario: 模型输出无效 JSON
- **WHEN** 模型返回无法解析或不符合候选 schema 的输出
- **THEN** 系统将 job 标记为 retryable/error，且 MUST NOT 对部分解析内容执行 memory side effect

#### Scenario: 自然语言记忆请求不依赖关键词旁路
- **WHEN** 用户以不同自然语言表达“记住”“以后”“忘记”或同等语义
- **THEN** 系统在普通形成批次中根据完整 turn evidence 识别候选，且 MUST NOT 因单一关键词直接写入或删除 memory

#### Scenario: Temporary mode 在缓冲前禁写
- **WHEN** 请求级 temporary/private policy 禁止记忆形成
- **THEN** 系统不把该 turn 正文加入 formation buffer，并记录不含敏感正文的 skipped trace

### Requirement: 结构化任务事件立即形成确定性投影
系统 SHALL 在 Plan、Run、Result 或 Artifact canonical 变更成功后立即生成幂等 structured-event formation job，不等待普通 turn window 或 idle。

#### Scenario: 未完成 Plan 产生 task projection
- **WHEN** Plan 被创建或状态/当前步骤发生变化
- **THEN** projector 从 stored Plan ownership 生成包含 `tenant_id/user_id`、deterministic `tenant:user:plan:slot` memory key、plan ID、status、next/current step 和 last activity 的 bounded `task_memory` candidate

#### Scenario: Result 或 Artifact 产生引用投影
- **WHEN** Agent Result 或 Artifact 被持久化
- **THEN** projector 只使用 canonical ID、status、bounded summary 和 normalized refs 生成 candidate，不复制无界正文

#### Scenario: 重复事件不重复投影
- **WHEN** 同一 canonical event/version 被重复投递
- **THEN** structured-event idempotency key 产生 NOOP 或复用既有 job，不创建第二个逻辑 memory revision

#### Scenario: 模型不得覆盖 canonical status
- **WHEN** projector 使用模型生成可读摘要
- **THEN** plan/run/result/artifact ownership、ID、status 和关联 MUST 来自 canonical repository，模型输出不能覆盖它们

#### Scenario: Structured Plan 缺少 ownership
- **WHEN** projector 收到不含 required user/tenant ownership 的 malformed Plan 或 event association
- **THEN** 系统拒绝 projection 并记录 bounded error，MUST NOT 生成 tenant-wide 或 unowned task memory

### Requirement: 普通形成不阻塞主对话响应
系统 SHALL 在 Turn Capsule 安全持久化后异步执行普通 formation job，且 route-and-invoke 响应不等待形成模型或 mem0 indexing。

#### Scenario: 五轮阈值在当前请求达到
- **WHEN** 当前 route-and-invoke 完成后刚好达到 5 个 pending turns
- **THEN** 系统在持久化 turn/job 后返回主响应，formation 状态通过独立 trace 查询或推送更新

#### Scenario: 形成任务超时或失败
- **WHEN** formation model、worker 或 mem0 indexing 超时/失败
- **THEN** 已完成的 Agent 响应保持成功状态，formation job 独立显示 retry/error/dead-letter

### Requirement: 自动形成支持可回滚 rollout
系统 SHALL 支持 `off`、`observe` 和 `enforced` formation mode，并在 runtime 中只暴露非敏感模式和健康状态。

#### Scenario: Formation mode off
- **WHEN** mode=`off`
- **THEN** 系统不创建自动 formation side effect，现有显式 memory write/recall 继续工作

#### Scenario: Formation mode observe
- **WHEN** mode=`observe`
- **THEN** 系统可以记录 turn/job、运行模型和 policy 并展示 decisions，但 MUST NOT 执行 ADD、UPDATE 或 DELETE

#### Scenario: Formation mode enforced
- **WHEN** mode=`enforced`
- **THEN** accepted lifecycle decisions 写入 canonical ledger 并创建对应 mem0 index operation
