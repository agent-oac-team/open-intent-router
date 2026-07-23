## Context

OAC 的业务运行观察需要回答真实请求正在做什么、为什么、使用了什么和得到什么，而不是展示技术日志或由模型生成流程说明。OIR 已维护多类 Canonical Data，但不同运行方拥有不同的事实边界：Core 知道 Turn、Route、Run、Result、Context 和 Memory；Host 知道 Session 可见性、页面交接和用户体验；Provider 只知道自身实际报告的阶段。

本变更采用联合投影。Execution Trace 不是新的权威状态机，也不是完整日志，它只按同一 Canonical Turn 关联已知的受治理事实。所有业务专有名称留在 Host 或 Provider Adapter，Core 只理解通用事件家族、关联、状态、有限事实和来源幂等身份。

## Goals / Non-Goals

**Goals:**

- 为真实 OAC Session 中的 Canonical Turn 提供有序、可恢复、可订阅的通用 Execution Trace。
- 用单表、同步、幂等 Writer 支撑当前五个 Golden Cases，且不让 Trace 写入失败回滚 Canonical Data。
- 保持 Snapshot 与 SSE 之间无漏读窗口，使用稳定的全局 `event_offset` 续传。
- 让 OAC 在验证本地 Session 存在且属于当前用户后，才能代理查询或订阅该 Session 的 Trace。
- 保证展示字段可以被确定性业务文案消费，且不存储凭证、Prompt、未治理上下文或 Provider 原始 Payload。

**Non-Goals:**

- 不新增 Trace 头表、Outbox、异步 Projector、消息队列、WebSocket、重试租约或死信机制。
- 不让 OIR Core 识别 OAC、IRS、Coze、Bot、页面路由或 Provider 节点名。
- 不把 Trace 作为删除、保留、执行控制、暂停、重跑或 Route 决策的事实源。
- 不实现跨用户审计、跨会话压缩、页面内业务回流或未接入 Golden Case 的平台化扩展。

## Decisions

### 1. 单表 append-only 事件存储

`execution_trace_events` 是唯一新增 Trace 数据表。每行包含：

- `event_offset`：PostgreSQL identity 生成的全局单调主键；
- `trace_id`、`tenant_id`、`user_id`、`session_id`、`turn_id` 和可选 `run_id`；
- `event_type`、`stage`、`status`、`reason_code`、`visibility` 和 `schema_version`；
- `source`、`source_event_id`、`source_version` 组成的来源幂等身份；
- 受限 `facts` 和 `evidence_refs` JSON；
- `occurred_at` 与 `recorded_at` 双时间戳。

`(source, source_event_id, source_version)` 唯一。重复来源事件返回现有行；同一来源身份携带不同关联或内容时拒绝为冲突，不能静默覆盖 append-only 事实。按 `(tenant_id, user_id, session_id, turn_id, event_offset)` 建索引，支持所有权范围内的 Snapshot 与 SSE 续传。

没有 `execution_traces` 头表。每个 Canonical Turn 的 Trace ID 由稳定的 Turn ID 派生，避免额外分配与生命周期。

### 2. 最小通用事件信封与白名单事实

固定事件家族为：`canonical_turn`、`context_pack`、`route_decision`、`agent_run`、`agent_event`、`agent_result`、`ui_handoff`、`memory_recall`、`memory_formation`、`memory_decision`、`memory_revision`、`trace_integrity`。生命周期仅通过 `stage` 和 `status` 表达。

Writer 对每个家族使用明确的 `facts` 键白名单。值只能是有限的标量、有限字符串列表或有限嵌套对象；禁止以任意 JSON 通道存储 `prompt`、`payload`、`token`、`authorization`、`secret`、`credential`、原始错误堆栈、完整上下文或 Provider 原始响应。Adapter 如需业务专有名称，只能把经过白名单确认的值放入适用的事实字段，例如 `provider_stage_name` 或 `target_route`；Core 不解释这些值。

### 3. 非阻断写入和 Trace Completeness

Trace 是观察投影。业务路径调用 Writer 时使用 `try_record`：Writer 错误被记录为低基数、脱敏的完整性缺口，不能回滚或改判已经成功的 Turn、Run、Result、Memory 或 UI Handoff。

一次写入失败后，当前业务响应和该 Trace 的 Snapshot/SSE 都报告 `completeness=incomplete` 与安全 `reason_code`。下一次可用写入会通过同一 Writer 追加 `trace_integrity` 事实；补写必须沿用原 `source/source_event_id/source_version`。查询可以从已有 Canonical Data 生成一个明确 `recovered=true` 的当前状态，但不得生成虚构的中间事件，也不得把恢复结果标为原始实时事件。

### 4. Snapshot + SSE 协议

查询入口先以 `(tenant_id, user_id, session_id, turn_id)` 读取 Snapshot，按 `event_offset` 升序返回事件、`watermark` 和 Trace Completeness。SSE 以同一所有权范围订阅，事件 `id` 是 `event_offset`，只发送 `event_offset > max(snapshot watermark, Last-Event-ID)` 的新记录。重连、重复或较旧游标不会改变顺序或重复返回已确认事件。

流建立和每次重连都先执行完整所有权校验；游标本身不授予访问权。第一版以短轮询等待新行实现服务端单向流，避免为没有双向需求的观察页面引入 WebSocket 或独立 broker。

### 5. Core 与 OAC 的边界

Core 暴露 `ExecutionTraceApplicationPort`，包含 `record`、`snapshot` 和 `stream`。Host Adapter 只能调用该端口及既有 Turn/Delegated Run/Event 端口。OAC Adapter 负责将受信 V2 身份映射为 tenant/user，拒绝 body 自报所有权；OAC Go 负责在调用 Adapter 前验证 OAC 数据库中 Session 仍存在且属于登录用户。

页面交接、OAC Session ID 到本地记录的关系、Provider 名称与业务文案都保留在 Adapter、Go 或 Client。Core 不引用这些模块。

### 6. 增量事实投影

首个纵向切片投影 `canonical_turn`、`route_decision`、`agent_run`、`agent_event`、`agent_result` 和 `trace_integrity`。后续 Golden Case 仅在真实路径已存在的基础上增加 `ui_handoff`、`context_pack`、`memory_recall`、`memory_formation`、`memory_decision` 和 `memory_revision`。每条投影使用已有 Canonical ID 作为来源事件 ID，不复制或新建第二套生命周期。

## Migration Plan

1. 通过 SQLAlchemy metadata 与 PostgreSQL schema 建立 `execution_trace_events`，迁移必须幂等且不修改既有 Canonical 表。
2. 先在 memory storage 与数据库存储实现相同的外部 Writer/Snapshot/SSE 契约，并用测试锁定来源幂等、顺序、所有权和白名单。
3. 将适配器入口接入当前 Central Route、Agent Event 和 Navigation 回调；仅接入真实链路，不以 fixture 或定时器伪造 Provider 阶段。
4. OAC Go 增加 Session 所有权代理，Client 通过该代理加载 Snapshot 和 SSE；删除 Session 后代理拒绝访问而不跨库删除 OIR Canonical Data。
5. 按 Golden Case 逐步接入剩余真实事实并保留不适用的事件家族为空，不在本变更中部署、切流或归档其他 change。

## Risks / Trade-offs

- 同步投影存在有限失败窗口：以显式完整性降级和幂等补写换取不引入 Outbox/Projector 的首期复杂度。
- 没有 Trace 头表：使用稳定 Turn 派生 Trace ID 和全局 offset，查询需要按关联索引过滤，但避免第二套生命周期。
- SSE 没有 broker：首期长轮询适合低并发观察工作台；未来可在 Writer 前加入 Outbox/Projector 而不改变事件、游标或客户端协议。
- Host 已验证 Session 所有权：OIR 不能独立得知 OAC Session 是否删除，因此 OAC Go 必须在每次 Snapshot 和 SSE 建连前重新验证。
