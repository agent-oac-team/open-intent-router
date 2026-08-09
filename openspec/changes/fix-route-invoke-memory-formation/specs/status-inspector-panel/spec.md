## MODIFIED Requirements

### Requirement: Evidence, Context, and Memory tabs are separated
The status inspector SHALL reserve separate tabs for Evidence, Context, and Memory state, and the Memory tab SHALL distinguish recall, Canonical Turn completion, Formation, lifecycle persistence, indexing, and terminal outcome.

#### Scenario: Evidence returned
- **WHEN** a route response contains evidence in route context
- **THEN** the Evidence tab displays the evidence payload or summary

#### Scenario: Context Pack not implemented yet
- **WHEN** the current response does not contain Context Pack data
- **THEN** the Context tab shows an explicit empty state without implying an error

#### Scenario: Memory trace is still progressing
- **WHEN** the current request has a non-terminal Turn, Outbox, Formation Job, or Index Operation
- **THEN** the Memory tab shows the current stage and continues bounded polling without discarding the last known state

#### Scenario: Memory formation is not eligible
- **WHEN** Formation is off, request policy suppresses memory, or policy rejects all candidates
- **THEN** the Memory tab shows the corresponding explicit skipped/rejected terminal reason instead of a generic empty state

#### Scenario: Memory is persisted
- **WHEN** a request produces a canonical Memory Item/Revision and the index operation is ready
- **THEN** the Memory tab shows persisted success and the correlated request/turn/job/memory identifiers

## ADDED Requirements

### Requirement: Memory 形成终态必须持续可见且可解释
测试台 SHALL 将 `skipped`、`completed_no_candidate`、`rejected`、`retry`、`dead_letter`、`index_pending`、`persisted` 作为明确状态展示，MUST NOT 仅因达到固定轮询次数就把未知状态改成 `not_triggered` 或移除状态反馈。

#### Scenario: 达到前端轮询上限但后端仍 pending
- **WHEN** 前端达到主动轮询上限且后端最后状态仍为 pending/running
- **THEN** 页面保留最后状态、更新时间和手动刷新入口，并标记为等待或超时未知而不是未触发

#### Scenario: 后端返回 dead-letter
- **WHEN** Formation 或 Index Job 重试耗尽
- **THEN** 页面持续显示 dead-letter、安全错误码、尝试次数和关联 Job ID

#### Scenario: 后端没有任何 Formation Trace
- **WHEN** request 已存在但查询不到 Turn/Outbox/Formation 关联
- **THEN** 页面显示 `trace_missing` 诊断状态并提示链路最后已知阶段，不把它解释为正常完成
