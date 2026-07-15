## Context

OIR 当前已经具备：

- `POST /api/v1/memories/write-candidates` 的显式候选治理和写入。
- `MemoryService.recall()`、`Mem0MemoryAdapter.search()` 和 Agent `memory_context` 召回。
- mem0/Milvus Lite `oir_memory_vectors` 派生索引，以及 PostgreSQL `memory_items` / `memory_events` canonical ledger。
- route/agent 两阶段 governed context pipeline，可在 Router 前受控读取 active Plan、Memory 和其他后端事实。
- 每轮 conversation trace 与只读 Memory Debug UI。

当前缺口是没有从完整对话 turn 或结构化任务事件进入长期记忆生命周期的自动闭环。现有 adapter 的 `extract()` 是透传，`add()` 未显式传 `infer=False`，没有 update 契约、revision ledger、durable idle trigger、冲突 pending 和用户/TTL 全链路硬删除。

当前 Plan ownership 也不足以支撑安全的跨会话 task memory：公开 `Plan` schema 没有 `user_id/tenant_id`，数据库只有未被 repository 实际使用的 nullable `user_id`，没有 `tenant_id`，Plan 查询主要按 `plan_id/session_id`。本 change 直接修正现有 Plan contract，不引入独立 `PlanRecord/PlanOwnership`，也不保留尚未正式使用的旧 Plan 兼容路径。

本设计锁定自托管 `mem0ai 2.0.11 + Milvus Lite`。该版本的 `infer=False` 会直接写每个非 system message，不做 OIR 所需的语义去重、更新、冲突或版本治理；因此形成和最终裁决必须在 OIR 内完成。PostgreSQL 继续是唯一 canonical store，mem0/Milvus 可由 active projection 重建。

约束：

- 一轮定义为同一请求关联的 user message、assistant/Agent response 和必要 Result/Plan/Event/Artifact refs。
- 普通对话默认 5 轮或 idle 30 秒形成；Host session close 不可靠。
- 结构化 Plan/Run/Result/Artifact 事件必须立即投影。
- 不增加 Router 功能意图类别。
- 模型只提出候选/操作，不能直接执行数据库或硬删除。
- 用户删除和 TTL 到期必须删除 current/revision 正文和 mem0 vector。
- 每个 Plan 必须具有服务端绑定的 `user_id + tenant_id`，模型、Host 和前端不得决定或覆盖 ownership。
- 当前工作树可能同时包含其他 change 的改动；实现必须沿用已完成的 governed context pipeline，而不是重建路由上下文路径。

## Goals / Non-Goals

**Goals:**

- 建立一个可由 turn window、idle、structured event、sweeper 和 manual command 复用的 `MemoryFormationPipeline`。
- 普通形成不阻塞主对话响应，结构化任务 projection 在事件持久化后立即产生。
- 使用严格结构化语义候选、确定性硬规则和保守 pending/verifier 边界完成证据、DLP、阈值、key、幂等、去重、冲突和删除权限治理；策略层不承担开放式自然语言理解。
- 建立 current projection、revision/superseded、formation job、index state 和安全审计数据模型。
- 让 mem0 只负责已治理 canonical memory 的向量存储、更新、删除和检索。
- 让未完成 Plan 可通过低权威 task projection 跨会话定位，同时不污染无关新任务。
- 让 Plan 创建、读取、确认、取消、执行、事件更新和 task projection 使用同一必填 owner/tenant 边界。
- 提供 selected turn recall trace、异步 formation decision、pending conflict、delete/index/worker 状态的 Debug/UI 可观测性。
- 支持重启恢复、幂等重试、跨租户隔离和从 PostgreSQL 重建向量索引。

**Non-Goals:**

- 不实现新的 Router 功能意图或记忆专用路由类别。
- 不把完整 transcript、Plan、Result 或 Artifact 正文写成长期 memory。
- 不让 memory 取代 Plan/Run/Result canonical repository。
- 不引入 mem0 Platform Graph、temporal、decay、criteria、webhook、export 或多模态能力。
- 不依赖 session close，不保证普通自然语言“记住”在下一轮前立即可召回。
- 不在首版使用任意语义 DELETE；DELETE 必须有明确用户证据或确定性生命周期原因。
- 不将 mem0 SDK SQLite history 作为 revision 或审计事实源。
- 不支持共享 Plan 或 tenant-wide 默认可见；未来共享能力必须引入显式成员 ACL。

## Decisions

### Decision 1: 只建设一个 MemoryFormationPipeline

所有来源先归一化为 `FormationJob -> MemoryCandidate[] -> PolicyDecision[] -> LifecycleOperation[]`：

```text
completed turn range ----> ConversationFormationModel --+
structured event --------> StructuredEventProjector ----+--> CandidatePolicy
manual management command ------------------------------+       |
sweeper/consolidation -----------------------------------+       v
                                                        MemoryLifecycleService
```

trigger 只决定何时评估，policy 决定是否以及如何改变长期记忆。pipeline 不按 P0/P1 拆成同步和异步两套实现。

替代方案：每轮 hot path 和固定窗口各建一套抽取服务。该方案会产生两套 prompt、阈值、幂等和 trace，长期行为无法一致。

### Decision 2: 完整 turn 先持久化为有界 TurnCapsule

在 `InvocationService` 完成 run/result 持久化后，由独立 coordinator 记录 `MemoryFormationTurn`：

- `turn_id/request_id/session_id/run_id/user_id/tenant_id/agent_id`
- bounded user/assistant text
- result/plan/artifact refs
- 本轮实际使用的 memory IDs
- result status、completed_at、formation status

user/assistant 原文使用独立配置限制；大 output、Artifact 正文和完整 `memory_context` 不进入 capsule。`used_memory_ids` 用于防止旧记忆经 Assistant 复述后自我强化。

失败或 invalid output 的 invocation 也可形成 capsule 供调试，但默认不从纯 assistant error 生成长期 user fact。route-only 没有完整 assistant/Agent result 时不进入普通形成；未来如有稳定 top-level assistant message，可通过同一 capsule builder 接入。

替代方案：形成时临时从 route logs、messages 和 results 拼接。该方案难以证明同一 turn 边界，且重试时会受后续消息变化影响。

### Decision 3: 使用数据库 watermark + lease job 处理 5 轮和 idle

新增持久化 `memory_formation_turns` 和 `memory_formation_jobs`（或 schema 等价实现）：

- 每个 session/tenant 维护最后 successful/claimed turn watermark 和 `idle_deadline_at`。
- 写入完整 turn 后，在同一事务中检查未 claimed turn 数量；达到 5 轮创建 `turn_window` job。
- sweeper 查询 `idle_deadline_at <= now` 且仍有 pending turns 的 session，创建 `idle` job。
- job 通过 `tenant_id + user_id + session_id + first_turn_id + last_turn_id + formation_policy_version` 唯一键幂等。
- worker 使用 lease owner/lease expiry/attempt/next_attempt_at，支持超时回收和 dead-letter。
- job 成功后推进 successful watermark；失败只释放 lease，不丢 turn。
- job 运行期间新 turn 不并入已冻结 range，进入下一窗口。

5-turn 和 idle 同时触发时，由唯一键和 claimed range 收敛为一个 job。Host close 将来只能创建同一 job 的早触发，不改变 correctness。

替代方案：进程内 `asyncio.sleep(30)`。该方案在多实例、重启和 deploy 时丢失 deadline，也无法防重复执行。

### Decision 4: 结构化事件使用确定性 projector，并走相同 lifecycle

Plan、Run、Result、Artifact repository 在 canonical 变更成功后发布内部 formation command。`StructuredEventProjector` 只从已持久化对象生成：

- 确定性 `memory_key`。
- canonical `tenant_id/user_id`、对象 ID、status、next step、last activity、bounded summary/ref。
- `authority=authoritative` 和 source event/version。

模型可选地压缩可读 summary，但不能生成或覆盖 ownership、ID、status 和关联。event projection 也创建幂等 formation job，trigger=`structured_event`，因此 revision、index 和 trace 不另走旁路。

替代方案：在 Plan/Event service 中直接调用 mem0 add。该方案绕过 revision、冲突、删除和 index outbox。

### Decision 5: 普通对话使用独立形成模型接口和严格 schema

新增 `ConversationFormationModel` protocol。默认实现复用现有 OpenAI-compatible LLM 基础设施，但使用独立 model/prompt/version/timeout 配置。输入是冻结 turn range 和有限 existing current projections；输出必须通过 Pydantic/JSON schema：

- proposed operation：ADD/UPDATE/DELETE/IGNORE
- scope、subject、memory key hint
- canonical content/structured value
- semantic target：`target`、`slot`、strict JSON `value`
- `temporal_scope`：current turn、session、long term 或 unknown
- `polarity`：affirmed、negated 或 unknown
- `certainty` 与 change/delete intent
- confidence、importance、sensitivity
- evidence turn/role/bounded quote
- optional target memory IDs 和 reason

解析失败、timeout 或 schema violation 使 job 可重试或 dead-letter，不产生 partially accepted memory。普通对话候选缺少结构化语义字段时不得进入自动 lifecycle。模型自检是一个信号，最终 policy 不信任模型提供的 tenant、subject identity、target memory ID 或 sensitivity。

自然语言“记住/忘记/以后/这次”、语言偏好、文档引用与多语言同义表达由 formation model 解释并投影为结构化语义；policy 只验证字段间一致性、证据引用真实性和硬规则，不使用持续扩张的关键词/正则充当开放式语义解析器。Request-level temporary/private flag 仍在进入 buffer 前确定性禁写。

替代方案：调用 mem0 `infer=True` 再把结果抄回 ledger。该方案让 provider prompt 绕过 OIR schema/policy，而且锁定版本不能提供所需 revision 和可靠 update/delete。

### Decision 6: Policy 拆成硬规则、结构化语义校验和不确定性处理三层

`MemoryCandidatePolicy` 只负责编排三个独立边界，最终仍以 PostgreSQL current projection 为准：

1. `MemoryCandidateHardRules` 执行不可委托给模型的确定性检查：从 job/request/canonical event 重建 tenant/user/subject，验证 scope、证据 ref 是否真实属于 frozen source、bounded content、DLP/sensitivity、target memory ID ownership/uniqueness、TTL/canonical lifecycle reason、memory key、candidate hash、幂等、去重和删除授权。
2. `MemoryCandidateSemanticValidator` 只消费 formation model/projector 已输出的结构化字段，验证 `target/slot/value/temporal_scope/polarity/certainty/change_intent` 与 proposed operation、scope、current projection 和 evidence role 是否一致。它不重新解析 evidence 自然语言，不维护多语言关键词或正则语义表。
3. `MemoryCandidatePolicy` 根据 hard-rule outcome、semantic validation、current projection 和配置阈值产生 NOOP/ADD/UPDATE/DELETE/REJECT/PENDING。语义字段缺失、unknown、互相冲突、与 current state 无法确定关系，或 verifier 没有给出确定结论时，一律 PENDING，不得自动 side effect。

固定决策顺序：

1. hard rules reject/noop/authorized lifecycle outcome。
2. structured semantic consistency outcome。
3. exact replay/hash/same value -> NOOP。
4. low confidence 或 hard policy violation -> REJECT。
5. medium confidence、unknown/ambiguous semantics 或 unresolved conflict -> PENDING。
6. 无 current 且 high-confidence long-term affirmed value -> ADD。
7. 有 current 且 high-confidence explicit long-term replacement -> UPDATE。
8. DELETE 仅在 hard rules 确认 explicit-user unique target，或 TTL/canonical lifecycle reason 成立时执行。

可选 `MemorySemanticVerifier` 是独立只读模型接口，只能对候选和 bounded evidence 返回 confirmed/contradicted/uncertain verdict；它不能调用 repository、lifecycle 或 provider。verifier 未配置、失败或返回 uncertain 时保持 PENDING，不能因 verifier 置信度直接越过 hard rules 自动写入。

mem0 semantic search 只可发现 potential duplicate，不能成为 current state 或最终裁决来源。阈值全部配置化，首版统一保守值，后续按 scope 评估校准。

替代方案：在 policy 内用不断增加的多语言正则重新解释 evidence。该方案会形成第二个不完整的自然语言模型，容易同时产生误写和正确路径回归，因此只允许保留范围明确、可删除的临时安全拦截，不能作为 ADD/UPDATE/DELETE 的授权依据。另一替代方案是让 formation/verifier 模型直接 tool-call lifecycle；该方案无法保证 tenant、删除权限和可重复测试。

### Decision 7: memory_key 和 revision 分离逻辑身份与内容

`memory_key` 表示 tenant 内的逻辑槽位，例如：

```text
tenant:t1:user:u1:preference:response_language
tenant:t1:user:u1:plan:plan_x:task_status
tenant:t1:artifact:artifact_x:reference
```

`candidate_hash` 表示规范化内容和 evidence refs，用于重放去重。`memory_id` 是逻辑对象 ID，在 UPDATE 中保持稳定；`revision_id/revision_no` 表示版本。

新增 `memory_revisions`：`revision_id, memory_id, revision_no, memory_key, operation, content, structured_value, evidence_refs, confidence, policy_version, supersedes_revision_id, created_at`。

扩展 `memory_items`：`memory_key, current_revision_id, lifecycle_status, index_status, formation_job_id`，并建立 tenant/subject/scope/memory_key 条件唯一约束。旧版本通过 `supersedes_revision_id` 和 current pointer 推导，不依赖 mem0 history。

替代方案：每次更新插入新的 `memory_items`。该方案会让 recall 同时看到冲突值，也无法稳定映射 external ID。

### Decision 8: canonical transaction 与 mem0 index operation 解耦

Lifecycle operation 先在 PostgreSQL 事务内完成 current/revision/event 和 index operation/outbox 状态，再由 worker 调用 mem0。这样数据库提交成功而进程崩溃时仍可恢复。

- ADD：写 current + revision 1，`index_status=pending`；worker 调用 mem0 add。
- UPDATE：写新 revision/current，`index_status=pending`；worker 使用已知 `mem0_memory_id` 调用 update。
- 成功：保存/adopt external ID，`index_status=ready`，记录 provider event。
- 失败：`index_status=out_of_sync`，按 retry policy 重试；不能报告 provider success。

mem0 add 强制传单条 `item.content` 和 `infer=False`。为缩小 add 成功但 external ID 未落库的 crash window，重试前按 OIR `memory_id` metadata 查找已存在 vector 并 adopt；repair job 扫描重复/孤儿映射。实现和 fake/real smoke 必须验证该行为。

本地开发可继续显示 degraded repository fallback；生产 fail-closed 行为沿用现有配置，但 canonical item 和 index status 必须真实可见。

替代方案：先调用 mem0 再提交 PostgreSQL。该方案在数据库失败时产生无 canonical owner 的孤儿 vector。

### Decision 9: 用户和 TTL 删除使用 deletion pending + 无正文 tombstone

DELETE 先唯一解析 OIR item，并在事务内标记 `deletion_pending`，使所有 canonical recall 和 governed projection 立即排除。删除 worker 冻结 external ID 并调用 mem0 delete：

1. mem0 delete 成功或确认不存在。
2. 删除该 `memory_id` 的所有 revision 正文。
3. 删除 current item 正文/记录。
4. 写不含 content、quote、structured value 的 tombstone event。

外部 delete 失败时 current 仍为 `deletion_pending` 且 fail-closed，不可召回；external ID 保留用于重试。达到 dead-letter 时管理面必须告警，不得恢复 active。

自然语言删除随普通 formation trigger 处理；管理 API/UI 删除为 deterministic command，可立即创建 delete operation。模型 DELETE 目标不唯一时只创建 pending decision。

TTL 使用 PostgreSQL 精确 `ttl_expires_at`。到期即在 recall 层排除，sweeper 创建相同 hard-delete operation。mem0 `expiration_date` 只作第二层隐藏，不能替代物理删除。

替代方案：只设置 expired flag 或只删 PostgreSQL。前者违反硬删除要求，后者会让 vector 再次被 mem0 search 返回。

### Decision 10: Plan 直接包含服务端控制的必填 ownership

现有 `Plan` schema 直接增加必填 `user_id` 和 `tenant_id`；`plans.user_id` 改为 NOT NULL，并新增 NOT NULL `plans.tenant_id`。不新增 `PlanRecord/PlanOwnership`，也不保留 nullable owner 或旧 Plan backfill 分支。

ownership 规则：

- Router/Plan 创建流程从当前受信 `UserContext` 绑定 `user_id/tenant_id`。
- 原始 LLM route output 在进入最终 `Plan` validation/persistence 前由 RouterService 注入并覆盖 trusted ownership；Prompt、LLM、Host 或 frontend 提供的同名值一律不可信。
- direct Plan execution/creation API 同样从认证 user context 绑定，缺少 user 或 tenant 时拒绝创建 Plan。
- repository `get/get_active_by_session` 和 Plan service `confirm/cancel/execute` 使用 `tenant_id + user_id` 过滤。
- Agent event 本身不重新指定 owner；系统先通过受信 run/session 关联找到已有 Plan，并继承 stored ownership 后更新。
- 当前范围只支持个人 Plan。相同 tenant 的其他用户默认无权读取、确认、取消、执行或形成该 Plan 的 task memory。

Plan-linked Agent execution 使用两层身份：短期 claim token 负责 repository CAS/lease fencing，稳定 execution idempotency key 负责下游副作用去重。OIR 在调用期间续租；lease 过期恢复同一 attempt 时复用 execution key，blocked 后显式恢复则创建新 attempt/key。外部调用语义是 at-least-once，不宣称 exactly-once；HTTP Agent 必须事务性处理 `Idempotency-Key`，local function 必须事务性处理 `invocation.context.plan_execution_idempotency_key`。进程内 single-flight 只降低同实例重复调用，不替代 Agent 的 durable 去重。

替代方案：新增独立 `PlanRecord/PlanOwnership`，保持公共 Plan 不变。当前项目尚未正式使用，直接强化现有 Plan 更简单，也能避免 DTO 与持久化 owner 漂移。

### Decision 11: task_memory 是 canonical Plan 的低权威投影

Plan projection 只包含 `tenant_id/user_id` ownership、bounded goal/status、`plan_id/current_step_id/next_step_id/last_activity_at`。Plan repository 是事实源，memory 只用于跨会话发现和定位。

- active/blocked/failed Plan event 立即 ADD/UPDATE 同一 `tenant:<tenant>:user:<user>:plan:<id>:task_status` key。
- completed/cancelled 立即更新为 terminal，并由 TTL/consolidation 清理。
- route-stage context provider 可以在“继续上次任务”等引用不完整时选择 active task projection。
- 当前用户输入和 canonical active Plan authority 高于历史 task memory。
- Agent 执行前必须按 `tenant_id + user_id + plan_id` 回读 canonical Plan。

本 change 不增加 intent，也不修改 Router action taxonomy。它只提供可过滤 projection 和 metadata；route/agent 选择继续使用 governed context pipeline。

替代方案：把完整 Plan 存进 memory 并让 Router 直接执行。该方案产生双事实源和陈旧步骤状态。

### Decision 12: consolidation 复用 policy 和 revision

后台 consolidation 按 tenant/user/subject/scope 分区：

- exact/semantic duplicate merge。
- session summary 压缩。
- 从 canonical Plan/Run/Result 修正 task projection。
- orphan vector/index mapping repair。
- conflict proposal 进入 pending。

consolidator 只能提出候选，仍走 CandidatePolicy 和 LifecycleService；不能批量直接覆盖 current。衰减/importance 只影响未来召回排序，不等于删除。

替代方案：定期重新生成整个用户 profile 并覆盖。该方案难以审计，且一次错误会破坏全部历史。

### Decision 13: formation trace 与 context trace 分开关联

Context Trace 回答“本轮模型实际使用了什么”；Formation Trace 回答“哪些 source turns/events 形成或拒绝了什么”。两者通过 `request_id/session_id/turn_id/run_id/memory_id` 关联，但不互相嵌入无界内容。

Formation Trace 至少包含：

- job ID、trigger、source turn range、版本、状态、attempt、latency/usage。
- candidate decision、memory key/ID/revision、reason code、provider/index status。
- ADD/UPDATE/DELETE/NOOP/REJECT/PENDING 数量。

`/memories/debug` 增加 request/session/turn/job/memory key 过滤。聊天 turn 保存真实 Recall Used；异步 formation 完成后通过轮询/SSE/refresh 回挂 source turn range，不修改历史 assistant 正文。全局 debug item 不能被 UI 推断为某轮实际使用。

替代方案：把 write trace 塞进原始 `RouteAndInvokeResponse`。普通 idle job 在响应后才发生，无法稳定满足该同步契约。

### Decision 14: 管理操作使用主体权限，不复用只读 debug 假设

新增用户级 memory delete/confirm/reject contract：

- 普通用户只能管理自己的 tenant/user/subject memory。
- 管理员跨主体操作继续要求 admin authentication，并记录 actor/reason。
- pending UPDATE/DELETE 展示 bounded preview 和 source refs；敏感拒绝不显示原文。
- delete/confirm/reject 使用 idempotency key，重复请求不重复变更。

现有 Debug view 的 read-only 能力保持；有副作用的按钮调用独立管理 endpoint，并显示 pending/provider completion 状态。

替代方案：直接让 `/memories/debug` 支持任意 delete。该方案混淆诊断和管理权限边界。

### Decision 15: rollout 分为 off/observe/enforced

新增 `MEMORY_FORMATION_MODE=off|observe|enforced`：

- `off`：不创建自动 formation job；显式 memory API 保持工作。
- `observe`：持久化 turn/job，运行形成和 policy，记录 decisions，但不执行 ADD/UPDATE/DELETE。
- `enforced`：执行生命周期操作和 mem0 index outbox。

独立开关控制 structured event projection、worker、sweeper 和 consolidation。runtime 只暴露模式、版本、队列深度、last error 等非敏感信息。

阈值、5-turn、30-second、timeouts、lease/retry、TTL/sweep intervals 配置化。默认先 observe，再在真实中文评估和 smoke 通过后 enforced。

替代方案：一次性启用全量自动写。现有形成质量和异步恢复尚无生产数据，无法安全回滚。

### Decision 16: 数据删除和日志默认不保存候选原文

formation turn/job payload 只保存执行所需 bounded 文本并使用短 TTL。Persistent events/logs 默认保存 source refs、hash、scope、reason 和 bounded redacted preview，不保存完整 candidate/quote/prompt。

用户/TTL hard delete 还必须清理：

- current/revisions 正文。
- 尚未完成 job/capsule 中与目标直接关联且可定位的正文。
- mem0/Milvus vector。
- UI cache 中的正文展示。

审计 tombstone 不含可恢复正文。密钥、authorization、连接凭证和 regulated data 在形成前后都执行 redaction/DLP。

替代方案：为可调试性永久保存形成 prompt 和完整输出。该方案扩大隐私面，并使用户删除无法闭环。

## Risks / Trade-offs

- [Risk] 5 轮/30 秒可能对某些会话过慢或过频 → 参数配置化，记录形成 precision、NOOP、纠正率和成本，后续按 scope/流量校准。
- [Risk] 形成模型生成错误 memory key 或高置信幻觉 → subject/key 由 policy 归一化，要求 user/canonical evidence，模型不能执行 side effect。
- [Risk] ADD 在 mem0 成功后进程崩溃造成重复 vector → metadata 写 OIR memory ID，重试先 adopt，repair job 扫描重复/孤儿，所有 recall 再做 canonical active 校验。
- [Risk] PostgreSQL current 已更新但 vector 尚未更新 → 暴露 `index_status`，生产按 fail-closed policy 处理并重试，不能把 pending 当 provider success。
- [Risk] mem0 delete 长时间失败导致敏感 vector 保留 → 立即停止 OIR recall、指数退避重试、dead-letter 告警，并提供受控 repair/delete 管理命令。
- [Risk] formation buffer 变成第二套 transcript → bounded fields、短 TTL、加密/权限、成功后清理，禁止无界 output 和 context copy。
- [Risk] active task memory 污染无关新任务 → derived authority、current input priority、relevance/budget selection、执行前回读 canonical Plan。
- [Risk] LLM、Host 或前端伪造 Plan ownership → 最终 Plan validation/persistence 前从受信 UserContext 强制覆盖，所有 repository/service 操作按 stored tenant/user 授权。
- [Risk] 多实例 sweeper 重复 claim → 数据库唯一约束、行锁/skip-locked、lease 和 frozen turn range。
- [Risk] Agent 在副作用后、Result 提交前遇到 executor/数据库故障 → OIR 续租并在同一 recovered attempt 复用稳定 idempotency key；Agent 必须按该 key 事务性去重，因为 claim fencing 本身不能提供跨系统 exactly-once。
- [Risk] PENDING 数量积压 → scope-specific metrics、过期策略、批量管理和保守自动 NOOP，不能自动升级为 UPDATE/DELETE。
- [Trade-off] 普通新记忆不会在下一轮必然可用 → 接受 eventual consistency，换取不阻塞主回复和更好的跨轮判断。
- [Trade-off] revision 增加存储成本 → revision 是纠错和审计必要成本；consolidation 不删除用户要求保留的历史，用户/TTL 删除时全部清理。

## Migration Plan

1. 直接扩展 `Plan` schema 为必填 `user_id/tenant_id`，将 `plans.user_id` 改为 NOT NULL 并新增 NOT NULL `plans.tenant_id`；开发环境不迁移或回填旧 Plan，必要时清空现有 Plan 测试数据后重建 schema。
2. 修改 Router/Plan creation、repository 和 service ownership 绑定/授权，再启用任何 structured Plan projection。
3. 新增 nullable memory lifecycle/index/current revision 字段、`memory_revisions`、formation turns/jobs/outbox 表和索引。
4. 为现有 active `memory_items` 回填 deterministic legacy memory key、revision 1、`lifecycle_status=active` 和已知 `index_status`，保留现有 `memory_id/mem0_memory_id`。
5. 扩展 memory repository 和 adapter；先让显式 write-candidates 使用 canonical text + `infer=False`，用 fake mem0 和真实 smoke 验证 add/search/update/delete。
6. 以 `MEMORY_FORMATION_MODE=off` 部署 schema/service，无自动副作用。
7. 启用 structured event/turn capture 和 `observe`，验证 job 幂等、候选分布、DLP、阈值和 Debug/UI。
8. 启用 worker/sweeper，在隔离 tenant 上 `enforced`，验证 5-turn、idle、restart、revision、hard delete 和 index repair。
9. 分 tenant 扩大 enforced，监控 queue depth、dead-letter、out-of-sync、错误写入/纠正率和 recall usage。
10. 最后启用 consolidation；高级阈值/priority 校准基于真实评估，不阻塞基础闭环。

回滚：切换 `MEMORY_FORMATION_MODE=off` 并停止 worker/sweeper，保留已写 canonical memory 和 revision，不删除新表。已有 memory 继续通过显式 API 和 recall 使用；正在 deletion pending 的记录保持 fail-closed，删除 worker 可单独运行到完成。adapter 的 `infer=False` canonical 写入不回滚为 provider 推理，以免重新引入双重形成。

## Open Questions

无阻塞设计问题。5-turn/30-second 和 `0.90/0.70` 是已确认的初始配置，不是永久质量结论；上线后需要用中文对话评估集和真实纠正/使用指标校准。
