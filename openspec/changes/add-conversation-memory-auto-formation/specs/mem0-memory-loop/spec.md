## ADDED Requirements

### Requirement: mem0 只存储 OIR 已治理的单条 canonical memory
系统 SHALL 对自动形成和显式候选使用单条 canonical memory content 调用 mem0 `add`，并 MUST 显式设置 `infer=False`，避免 mem0 再次抽取或拆分 transcript。

#### Scenario: 自动 ADD 写入 mem0
- **WHEN** OIR lifecycle 提交一个 accepted ADD operation
- **THEN** adapter 只发送该 current memory 的 canonical content、scope/tenant/subject/memory/revision metadata 和 `infer=False`

#### Scenario: Turn messages 不直接传给 infer false
- **WHEN** formation job 包含多个 user/assistant messages
- **THEN** adapter MUST NOT 把完整 message list 作为 `infer=False` payload，否则每条消息会成为独立 raw memory

#### Scenario: Explicit write-candidates 保持兼容
- **WHEN** 调用方通过现有 `write-candidates` API 提交已治理候选
- **THEN** accepted candidate 继续返回兼容 decision，同时 mem0 add 使用 canonical content 和 `infer=False`

### Requirement: mem0 adapter 支持按 external ID 更新 canonical memory
系统 SHALL 在 OIR current revision 更新时使用已知 `mem0_memory_id` 调用 OSS update，并 SHALL 保持 OIR `memory_id` 为逻辑主键。

#### Scenario: 已知 external ID 的 UPDATE
- **WHEN** accepted UPDATE 对应 current item 已保存 `mem0_memory_id`
- **THEN** adapter 调用 mem0 update 更新 data 和允许的 metadata，不新增第二个逻辑 OIR memory

#### Scenario: External ID 缺失
- **WHEN** UPDATE 没有可靠 external mapping
- **THEN** 系统标记 index out-of-sync 并进入 repair/adopt 流程，MUST NOT 把未知 provider 状态报告为成功

### Requirement: mem0 搜索结果受 canonical lifecycle 校验
系统 SHALL 在将 mem0 search 结果暴露给 `memory_context` 前验证对应 OIR current item 仍 active、未到期、未 deletion pending 且 tenant/subject 匹配。

#### Scenario: mem0 返回已删除或待删除 vector
- **WHEN** provider search 暂时返回无 active canonical item 或 lifecycle status=`deletion_pending` 的记录
- **THEN** OIR 过滤该结果并记录 stale/orphan index signal

#### Scenario: mem0 返回过期 vector
- **WHEN** provider search 返回 `ttl_expires_at <= now` 的 memory
- **THEN** OIR 不将其放入 memory context，并允许 sweeper/repair 删除 vector

## MODIFIED Requirements

### Requirement: mem0 写入经过 OIR 治理
系统 SHALL 先由 OIR formation/projector 和确定性 policy 形成、分类并裁决记忆候选，再把已接受的 canonical lifecycle operation 发送给 mem0；mem0 MUST NOT 拥有最终形成或删除权限。

#### Scenario: 接受的新候选写入 mem0 和 OIR metadata
- **WHEN** 低风险记忆候选通过 OIR evidence、confidence、sensitivity、scope、tenant、memory key 和 visibility policy 并产生 ADD
- **THEN** 系统先持久化 canonical current/revision/outbox，再以 `infer=False` 将 canonical content 和治理 metadata 写入 mem0

#### Scenario: 接受的更新写入同一 external memory
- **WHEN** 明确新值通过 OIR policy 并产生 UPDATE
- **THEN** 系统创建 OIR revision/superseded 关系，并按已知 mem0 ID 更新派生记录

#### Scenario: 拒绝或 pending 候选不写入 mem0
- **WHEN** 记忆候选被 OIR policy 判为 REJECT、NOOP 或 PENDING
- **THEN** 系统 MUST NOT 为该候选调用 mem0 add/update/delete，并 SHALL 返回或记录带 reason 的 decision

#### Scenario: 模型提出删除
- **WHEN** formation model 提出 DELETE 但确定性 policy 未确认唯一授权目标
- **THEN** adapter MUST NOT 调用 mem0 delete

### Requirement: mem0 ID 与 OIR ID 可追踪
系统 SHALL 保留稳定 OIR memory ID、current revision 和 mem0 external ID 之间的可追踪关系，并记录 provider/index 状态。

#### Scenario: mem0 add 返回自己的 memory ID
- **WHEN** mem0 add 返回的 ID 与 OIR `memory_id` 不同
- **THEN** 系统在 canonical mapping 中保存 mem0 ID，但不替换 OIR memory ID 或 revision identity

#### Scenario: mem0 search 返回 external ID
- **WHEN** mem0 search 返回匹配记忆
- **THEN** OIR 使用 metadata 中的 OIR memory ID 回查 active canonical state，并在 debug trace 中保留 external mapping

#### Scenario: 更新时保持映射
- **WHEN** OIR current revision 被更新
- **THEN** adapter 优先复用已知 mem0 ID，并记录 update status、revision 和 timestamp

#### Scenario: 清理时删除外部记忆记录
- **WHEN** 用户删除、TTL 或 lifecycle policy 删除带有已知 mem0 ID 的记忆项
- **THEN** 系统创建可重试 external delete operation，并将成功、失败或不存在状态记录为可观测 event

#### Scenario: 映射异常可修复
- **WHEN** external ID 缺失、重复或指向无 active canonical item
- **THEN** repair 流程按 OIR memory ID metadata adopt、去重或删除 provider record，且 PostgreSQL current state 保持 canonical

