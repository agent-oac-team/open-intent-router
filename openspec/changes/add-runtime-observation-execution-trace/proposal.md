## Why

OIR 已拥有 Canonical Turn、Route Decision、Agent Run、Agent Event、Agent Result、Context 和 Memory 等运行事实，但这些事实尚未以同一轮次和稳定游标提供给 OAC 的真实会话观察。现有 OIR Web 运行图主要在请求结束后还原信息，不能作为 OAC 业务运行观察的实时事实来源。

需要新增一个宿主无关的 Execution Trace 投影：OIR Core 只定义和保存受治理的通用事件，OAC Host Adapter 与执行方只写入各自真实掌握的边界事实。该能力必须首先支撑测试环境的五个 Golden Cases，但不能为尚未使用的场景预建 Trace 头表、Outbox、Projector、消息队列或 OAC/Coze 专有模型。

## What Changes

- 新增通用 `Execution Trace` 事件信封、12 个固定事实家族、按家族校验的有界 `facts` 白名单，以及独立 Trace Event Writer 应用端口。
- 使用一张 append-only `execution_trace_events` 表保存事件；全局单调 `event_offset` 同时作为读取顺序、快照水位和 SSE `Last-Event-ID` 游标。
- 提供按 Canonical Turn 严格所有权查询的 Snapshot 与 SSE 增量流；Snapshot 返回覆盖到的水位，SSE 仅返回水位之后的新事件。
- 将 Trace 写入定义为非阻断观察投影：业务事实提交不因 Trace 失败回滚，但响应、Snapshot 和流必须显式暴露 `Trace Completeness`；成功后的补写使用原来源幂等身份，恢复状态必须明确标识为 Recovered Snapshot。
- 在 OAC Host Adapter 中增加仅依赖应用端口的 Runtime Observation 入口，供 OAC Go 代理在验证真实 OAC Session 所有权后调用；Core 不获得 OAC、IRS、Coze、页面或 Provider 专有语义。
- 逐步把 Canonical Turn、Route Decision、Agent Run/Event/Result、UI Handoff、Context/Recall 与 Memory Formation 的现有真实事实映射进同一 Trace，优先保证已接入 Golden Case 的事实完整性。

## Capabilities

### New Capabilities

- `execution-trace`: 通用执行轨迹事件、单表持久化、幂等写入、Trace Completeness、恢复快照、Snapshot 与 SSE 续传。
- `oac-runtime-observation-bridge`: OAC Host Adapter 的受信查询/订阅入口及 OAC Session 所有权代理边界。

### Modified Capabilities

- `canonical-conversation-turn`: Canonical Turn、Route Decision、Run 和 Result 可以向通用 Execution Trace 投影真实事件，但 Trace 失败不能改变其权威状态。
- `delegated-external-run`: Delegated Run 的启动、进度、终态和 Ticket 所有权可投影为通用 Trace 事件，不改变既有 Ticket 或状态机语义。
- `memory-context-governance`: 已授权的 Context、Recall、Formation、Decision 和 Revision 可以投影有限事实，不能复制原始上下文、Provider Payload 或敏感正文。

## Impact

- OIR Core：新增 schema、repository、writer/query service、数据库模型与受影响生命周期的非阻断投影。
- OIR Host Adapter：新增受信的 Snapshot/SSE 入口和 UI Handoff/Provider 事实映射，仍只能调用 `app.application` 端口。
- OAC：Go 后端验证登录用户对仍存在 Session 的所有权后代理 Trace；Client 在独立三栏工作台显示确定性业务说明、业务证据和脱敏技术证据。
- 测试：增加 OIR API/数据库契约、OAC HTTP 所有权边界、SSE 续传、重复来源事件、失败降级与浏览器本地联调覆盖。
