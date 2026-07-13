## ADDED Requirements

### Requirement: OIR 确定性 policy 拥有最终记忆裁决权
系统 SHALL 只允许确定性 OIR policy 将形成模型或 projector 的候选转换为 ADD、UPDATE、DELETE、NOOP、REJECT 或 CONFLICT_PENDING，模型输出本身 MUST NOT 执行 side effect。

#### Scenario: 高置信低风险新候选
- **WHEN** 候选 evidence、scope、subject、tenant 和 sensitivity 均通过且 confidence >= 0.90，并且不存在同 memory key current projection
- **THEN** policy 产生 ADD decision

#### Scenario: 中等置信候选
- **WHEN** `0.70 <= confidence < 0.90`
- **THEN** policy 产生 CONFLICT_PENDING/PENDING decision，且 MUST NOT 自动改变 current memory

#### Scenario: 低置信候选
- **WHEN** confidence < 0.70
- **THEN** policy 产生 REJECT decision

#### Scenario: 模型提供跨租户目标
- **WHEN** 候选 subject、target memory 或 tenant 与 job/canonical identity 不一致
- **THEN** policy 从可信上下文重建 identity 或拒绝候选，且 MUST NOT 访问其他 tenant 的 memory

### Requirement: 长期记忆必须有可验证证据和敏感信息治理
系统 SHALL 要求普通对话候选引用本批次用户 evidence 或 canonical event，并在写入前执行 DLP/sensitivity policy。

#### Scenario: 仅来自 Assistant 的用户事实
- **WHEN** 候选声称用户长期事实但 evidence 只来自 Assistant/Agent 输出
- **THEN** 系统拒绝该候选

#### Scenario: 旧 recall 被复述
- **WHEN** 候选内容只重复 Turn Capsule 中 `used_memory_ids` 对应的旧记忆且没有新用户 evidence
- **THEN** 系统产生 NOOP 或 REJECT，不增加 confidence 或新 revision

#### Scenario: Secret 或 regulated data
- **WHEN** 候选包含 credential、token、password、验证码、私钥、金融账户、证件或未授权 regulated data
- **THEN** 系统拒绝候选，且审计/Debug MUST NOT 保存或展示原始敏感值

### Requirement: Memory key 与 candidate hash 分别支持逻辑身份和幂等
系统 SHALL 为每个 current memory 生成 tenant-scoped `memory_key`，并为候选生成包含规范化内容和 evidence refs 的 `candidate_hash`。

#### Scenario: 结构化 projection key
- **WHEN** candidate 来自 Plan、Result 或 Artifact
- **THEN** memory key 由 tenant、owner user、canonical object type/ID 和 projection slot 确定性生成；个人 Plan key 至少包含 `tenant_id + user_id + plan_id + slot`

#### Scenario: 相同 candidate 重放
- **WHEN** 相同 job/candidate hash 再次执行
- **THEN** 系统产生 NOOP，且不新增 memory item、revision 或 mem0 index operation

#### Scenario: 相同 key 且值相同
- **WHEN** current projection 与新候选具有相同 memory key 和规范化值
- **THEN** 系统产生 same-value NOOP，并可记录新的 evidence ref 而不创建冲突 item

### Requirement: Current projection 与 revision history 分离
系统 SHALL 使用稳定 `memory_id` 表示逻辑 memory，使用 `memory_items` 保存 current projection，并使用独立 `memory_revisions` 保存 ADD、UPDATE 和 CONSOLIDATE 版本链。

#### Scenario: 新增 memory
- **WHEN** ADD decision 被提交
- **THEN** 系统创建 current memory item、revision 1、current revision pointer 和 canonical event

#### Scenario: 更新 memory
- **WHEN** 同 memory key 的明确新值以 confidence >= 0.90 通过 policy
- **THEN** 系统保持原 `memory_id`，创建递增 revision，设置 `supersedes_revision_id`，并更新 current projection

#### Scenario: 并发更新同一 key
- **WHEN** 两个 worker 并发更新同一 tenant/subject/scope/memory key
- **THEN** 行锁/唯一约束和 revision sequencing 保证只有有效顺序的 current revision，不产生两个 active current items

#### Scenario: Revision 不依赖 mem0 history
- **WHEN** mem0 SDK history 不可用、使用 SQLite 或内容与 OIR event 不一致
- **THEN** PostgreSQL `memory_revisions` / `memory_events` 仍是版本和审计事实源

### Requirement: 去重和冲突在 lifecycle side effect 前完成
系统 SHALL 依次执行 operation idempotency、exact hash、current key/value 和受控 semantic potential-match 检查，再决定 lifecycle operation。

#### Scenario: Semantic search 发现近似项
- **WHEN** mem0 search 返回语义相似 memory
- **THEN** 系统只将其作为 potential duplicate，并根据 PostgreSQL current identity/evidence 作最终 NOOP、UPDATE 或 PENDING 决策

#### Scenario: 当前轮临时覆盖长期偏好
- **WHEN** 用户说“这次使用英文”而 current memory 表示长期偏好中文
- **THEN** 当前输入只覆盖本轮，policy MUST NOT 自动更新长期 preference

#### Scenario: 明确长期偏好变化
- **WHEN** 用户以明确 evidence 表示“以后都使用英文”且达到自动阈值
- **THEN** policy 可以 UPDATE 同一 preference memory key 并 supersede 旧 revision

#### Scenario: 含糊冲突
- **WHEN** 新候选与 current value 冲突但无法证明长期变化
- **THEN** 系统创建 CONFLICT_PENDING decision，不同时暴露两个 active values

### Requirement: 用户删除必须唯一授权并硬删除全部正文
系统 SHALL 只在用户/管理员有权管理目标且目标唯一时执行 delete，并 SHALL 删除 current、全部 revisions 和 mem0 vector 的正文数据。

#### Scenario: 用户删除自己的唯一 memory
- **WHEN** 用户通过管理 API/UI 或具有明确唯一 evidence 的自然语言请求删除自己的 memory
- **THEN** 系统创建 idempotent deletion operation，将目标置为 deletion pending 并立即停止召回

#### Scenario: 删除目标不唯一
- **WHEN** 自然语言 DELETE 可能匹配多个 memory 或跨 subject
- **THEN** 系统创建 PENDING decision 并 MUST NOT 删除任一目标

#### Scenario: Provider 删除成功
- **WHEN** mem0 delete 成功或确认 external memory 不存在
- **THEN** 系统硬删除 `memory_items` current 正文/记录和全部 `memory_revisions` 正文，并记录无正文 tombstone

#### Scenario: Provider 删除失败
- **WHEN** mem0 delete 暂时失败
- **THEN** memory 保持 deletion pending、不可召回并进入重试；用于重试的 external ID 保留，正文和失败状态不得重新变为 active

#### Scenario: 重复删除请求
- **WHEN** 同一 actor/idempotency key 再次请求删除同一 memory
- **THEN** 系统返回现有 pending/completed 状态，不创建重复 deletion operation

### Requirement: TTL 到期执行不可召回和物理删除
系统 SHALL 以 PostgreSQL `ttl_expires_at` 为 canonical 到期时间，到期即排除 recall，并由 sweeper 执行与用户删除相同的硬删除流程。

#### Scenario: TTL 刚到期
- **WHEN** current time >= memory `ttl_expires_at`
- **THEN** OIR recall MUST NOT 返回该 memory，即使 mem0 vector 尚未完成物理删除

#### Scenario: TTL sweeper 清理
- **WHEN** sweeper claim 到期 memory
- **THEN** 系统创建 reason=`ttl_expired` 的 deletion operation，删除 revisions/current 正文和 mem0 vector

#### Scenario: mem0 expiration 只隐藏内容
- **WHEN** mem0 `expiration_date` 已设置但没有执行 OIR deletion operation
- **THEN** 系统仍将该 memory 视为需要物理清理，不能把 provider 隐藏等同完成删除

### Requirement: Canonical state 与派生向量索引可恢复一致
系统 SHALL 为 ADD/UPDATE/DELETE 记录 index operation/status，并 SHALL 允许从 PostgreSQL active projection 修复或重建 mem0/Milvus 索引。

#### Scenario: Canonical commit 后 mem0 add/update 失败
- **WHEN** current/revision 已提交但 provider operation 失败
- **THEN** memory 标记 `index_status=out_of_sync`，记录可观测错误并创建 retry，而不是报告 provider success

#### Scenario: Add 成功后 external ID 尚未落库
- **WHEN** worker 在 mem0 add 成功后、保存 mapping 前崩溃
- **THEN** 重试/repair 使用 OIR memory ID metadata adopt 已有 vector 或去除重复，最终只保留一个有效映射

#### Scenario: 从 ledger 重建索引
- **WHEN** `oir_memory_vectors` 丢失或 provider 被迁移
- **THEN** repair/reindex 仅从 active canonical `memory_items` 和 current revisions 重建，不从 mem0 history 推断正文

#### Scenario: Orphan vector
- **WHEN** mem0/Milvus 中存在无 active canonical OIR memory 的 vector
- **THEN** repair 将其标记并安全删除，不把它重新导入为 active memory

### Requirement: 后台 consolidation 保持 policy 和 revision 语义
系统 SHALL 让 consolidation 只产生候选并复用正常 policy/lifecycle，而不是直接批量覆盖 current memory。

#### Scenario: 合并重复 memory
- **WHEN** consolidation 在同 tenant/subject/scope 发现 exact 或可信 semantic duplicates
- **THEN** 合并结果通过 policy 后创建 revision/superseded 关系，并保持一个 current projection

#### Scenario: 压缩 session summary
- **WHEN** 多个 session summaries 需要压缩
- **THEN** 系统可以生成新的 summary revision，但 MUST NOT 将 summary 中无 user/canonical evidence 的内容升级为 stable fact

#### Scenario: 修正陈旧 task projection
- **WHEN** task memory 与 canonical Plan/Run/Result 状态不一致
- **THEN** canonical state 产生 authoritative UPDATE/DELETE candidate 并覆盖 derived projection

#### Scenario: Consolidation 发现高风险冲突
- **WHEN** consolidation 发现 regulated/high-risk 或证据不足的冲突
- **THEN** 系统创建 PENDING decision，MUST NOT 自动覆盖或删除

### Requirement: Memory 生命周期严格隔离 tenant 和主体
系统 SHALL 在 memory key、repository query、formation/consolidation partition、mem0 metadata/filter 和管理授权中同时使用 tenant/user/subject identity。

#### Scenario: Tenant A 与 Tenant B 内容相似
- **WHEN** 两个 tenant 有语义和 memory key hint 相似的候选
- **THEN** dedupe、conflict、update 和 consolidation MUST NOT 跨 tenant 比较或合并 current memory

#### Scenario: 管理员跨 tenant 操作
- **WHEN** 已认证管理员管理其他 tenant 的 memory
- **THEN** 系统验证 admin permission、记录 actor/target/reason，并仍按目标 tenant 执行 lifecycle operation
