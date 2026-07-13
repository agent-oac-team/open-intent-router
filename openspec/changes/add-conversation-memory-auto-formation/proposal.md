## Why

OIR 已具备 mem0 显式写入和召回，但 `route-and-invoke` 及 Plan/Run/Result/Artifact 生命周期还没有自动形成、更新、去重、版本和删除闭环。需要在不增加 Router 功能意图、不把 mem0 变成 canonical store 的前提下，让跨会话偏好和未完成任务状态可形成、可追踪、可纠正并可彻底删除。

## What Changes

- 新增统一 `MemoryFormationPipeline`，普通对话使用完整 turn capsule，在默认 5 轮或空闲 30 秒时异步形成；Host session close 不作为正确性依赖。
- Plan、Run、Result、Artifact 等结构化事件产生时立即生成确定性 memory projection，并只保存 bounded summary、状态和 canonical ID 指针。
- 由 OIR 形成模型提出普通对话候选和操作建议，再由确定性 policy 执行证据、敏感信息、置信度、tenant、memory key、幂等、去重、冲突和删除权限检查。
- 新增 current projection + revision/superseded 生命周期：`memory_items` 保存当前状态，`memory_revisions` 保存版本链，`memory_events` 保存不含敏感正文的审计事件。
- 用户删除和 TTL 到期执行硬删除：立即停止召回，删除 current/revision 正文及 mem0 vector，只保留无正文 tombstone；外部删除失败时 fail-closed 并可重试。
- 修改 mem0 adapter 契约：已治理的单条 canonical memory 使用 `add(..., infer=False)`，更新使用 external ID 调用 `update`，删除和 index repair 保留 OIR/mem0 ID 映射。
- 增加持久化 formation watermark/job/outbox、竞态幂等、retry、dead-letter、consolidation 和派生索引修复能力。
- 扩展 Debug/API/UI，使每轮可看到实际使用的 recall memory，并按 source turns/job 展示 ADD、UPDATE、DELETE、NOOP、REJECT、PENDING 和 provider 状态。
- **BREAKING**：直接扩展现有 `Plan`，要求每个 Plan 都包含服务端绑定且不可由模型/Host 覆盖的 `user_id` 和 `tenant_id`；Plan 查询、确认、取消、执行和事件更新都必须校验 ownership。本项目尚未正式使用，不保留旧 Plan 兼容或回填分支。
- 未完成 Plan 形成带 `tenant_id + user_id + plan_id` 的低权威 `task_memory` 指针，支持 Router 前上下文理解“继续上次任务”；当前输入仍优先，不能让旧任务覆盖无关新任务或改变功能意图体系。
- 首版范围锁定自托管 `mem0ai 2.0.11 + Milvus Lite`；不引入 Graph、reranker、criteria retrieval、多模态或托管 Platform 专属能力。

## Capabilities

### New Capabilities

- `conversation-memory-formation`: 定义 turn capsule、5-turn/30-second idle、结构化事件即时 projection、形成模型、持久化触发、幂等和异步执行语义。
- `memory-revision-lifecycle`: 定义 memory key、候选状态机、current/revision/superseded、冲突 pending、用户/TTL 硬删除、consolidation 和索引一致性语义。

### Modified Capabilities

- `mem0-memory-loop`: 将 mem0 从推理形成者收敛为 `infer=False` 的派生存储/检索 adapter，并增加 update、delete、external mapping 和 repair 要求。
- `memory-context-governance`: 将候选形成和最终生命周期裁决明确归 OIR 所有，补充 task projection、证据/阈值、当前输入优先和硬删除治理要求。
- `observability-and-admin`: 增加 formation job/decision/revision/index/delete 的安全查询、过滤、管理和审计要求。
- `chat-conversation-console`: 增加 selected turn 的 Recall Used 和异步 Formation/Write Decisions 展示，禁止用全局 debug 状态冒充本轮 trace。
- `plan-orchestration`: 为现有 Plan 增加服务端控制的必填 user/tenant ownership，并要求所有读取和状态变更按 ownership 授权。

## Impact

- Backend schemas/services：必填 Plan ownership、memory candidate/decision、turn capsule、formation job/trace、`MemoryFormationPipeline`、trigger coordinator、event projector、lifecycle service、`MemoryService` 和 adapter protocol。
- Persistence：`plans.user_id` 改为必填并新增必填 `plans.tenant_id`；新增 `memory_revisions`、formation job/watermark/outbox 或等价持久化；扩展 `memory_items` lifecycle/index/current revision 字段和索引；更新 PostgreSQL schema/repositories。
- Invocation/events：在 AgentRun/AgentResult 持久化后记录完整 turn capsule；Plan/Run/Result/Artifact event 触发即时 projection。
- Provider：锁定 `mem0ai 2.0.11` 行为，Milvus Lite `oir_memory_vectors` 继续作为可重建派生索引。
- API/Debug/UI：扩展 memory debug filters、formation/revision trace、pending/delete 管理和每轮状态 inspector。
- Configuration：新增 formation enabled、window/idle、阈值、worker/retry、sweeper/consolidation 和 rollout 配置，runtime 只暴露非敏感状态。
- Tests：增加触发竞态、重启恢复、候选治理、revision、硬删除、跨租户、任务续接隔离、真实 mem0/Milvus smoke 和前端 selected-turn trace 验收。
- Public behavior：不增加 Router 功能意图类别；现有显式 memory API 和 `memory_context` 保持兼容；Plan contract 有意要求必填 `user_id/tenant_id`，不提供旧 Plan 兼容。
