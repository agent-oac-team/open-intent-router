# Execution Ticket 字段位置决策

## 决策

- Central Route 响应：顶层可选 `execution_ticket?: string | null`。
- Agent Event 请求：顶层可选 `execution_ticket?: string | null`。
- OAC 前端运行态：保存在当前 `LocalMessage`、`ActiveAgent` 和失败重试对象的专用可选字段中。
- OAC Go Central Proxy：保持不解析的字节流透传，不把 Ticket 写入日志。

Ticket 不放入 `route`，因为它不是路由业务决策；不放入 `context/frontend_context/output/metadata`，因为这些字段可能进入 Prompt、持久化正文、调试输出或被业务代码改写。Ticket 也不进入 OIR Core 公共 Schema，Adapter 验证后只向 Core 提交通用 execution reference。

## 生命周期

1. `open_agent/continue_agent` handoff 返回 Ticket；无外部执行的 action 不返回。
2. OAC 原样保存 Ticket，并在该 handoff 的 progress/final/error Event 中回传。
3. 最终 Event 被 Adapter/Core 幂等接受后清除当前 Ticket。
4. 网络失败且提交结果未知时保留 Ticket供同一 Event 幂等重试。
5. 用户点击失败重试时先重新 Route，新 Run 获得新 Ticket，旧 Ticket 不跨 Run 复用。

## 向后兼容

- 新类型必须接受旧 Route 响应中没有 Ticket。
- Adapter 必须接受旧 Agent Event 没有 Ticket，但只能使用已确认的唯一受信关联过渡规则。
- TypeScript 对响应额外字段天然兼容，Ticket 仍显式加入共享类型以防前端丢失。
- IRS 当前 `AgentEvent` 是 `extra=forbid`，直接发送含 Ticket 的 Event 会返回 `422`。因此 Ticketed write 只能发给 Adapter，禁止写 fallback 或同时发送 IRS。
