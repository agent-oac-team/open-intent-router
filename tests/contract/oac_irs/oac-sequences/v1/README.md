# OAC 中控可执行时序基线

`sequences.json` 固化 OAC AI Sidebar 经 Go Proxy 调用中控的 6 条核心时序：

1. 新请求直接回复。
2. 新请求打开非聊天 Agent。
3. 续接已有聊天 Agent。
4. Plan 确认并启动首步。
5. Agent 完成 Event 回流并继续路由。
6. Agent 失败后由用户触发、重新经过中控的重试。

每个 HTTP step 同时记录 OAC 公开路径和 IRS 目标路径。`scripts/validate_oac_sequence_fixtures.py` 校验协议版本、序列/步骤唯一性、include 引用和 `mapCentralProxyPath` 等价映射。后续 Shadow/E2E runner 可以展开 include、替换 `${variable}`，再按 step 顺序调用目标 Host。

## 已知旧语义

当前 OAC 聊天 Agent 在 Coze 返回非空文本后发送：

```json
{"event_type":"agent_result","status":"running"}
```

这表示“当前聊天轮次已有最终文本，但 Agent 会话仍可继续”，不是“当前轮次没有结果”。只有 Router 明确返回 `exit_agent` 时，OAC 才发送 `status=completed`。

迁移后的 Adapter 必须用 Ticket 绑定的 Delegated Run 区分“单轮 Run 完成”和“Agent 会话/Plan Step 是否结束”：

- `agent_result` 可收口 Ticket 对应的当前 Run/Turn Result。
- `status=running` 不得被误投影为 Plan Step 已完成。
- `status=completed` 才允许按所有权和关联关系推进或结束对应 Plan Step。
- 该兼容规则不得只依赖 `session_id + agent_id` 猜测 Run。

## 源码证据

- OAC Client 请求类型与四类 Central 调用：`apps/client/src/lib/central-api.ts`
- Route 结果、Agent 执行、Event 回流、Plan 确认与失败重试：`apps/client/src/components/ai-sidebar.tsx`
- OAC 到 IRS 的路径映射与并发保护：`apps/server/main.go`
