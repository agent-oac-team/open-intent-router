## Why

当前启用记忆需要同时协调 Recall、Formation、execution mode、Outbox consumer、多个 worker/sweeper 以及路由 Context 开关，任一遗漏都会形成“部分开启”状态。现有本地 OAC 迁移环境已经出现全局 Formation 为 `enforced`、但路由记忆关闭且全部 9 个 Agent 仍为 `memory.disabled` 的配置断层，因此需要用一个稳定的行为模式消除组合爆炸，同时保留 Agent 级最小授权。

## What Changes

- 新增唯一面向部署者的记忆行为开关 `MEMORY_MODE=off|observe|on`。
- 由 `MEMORY_MODE` 在代码中确定 Recall、Formation、execution、Outbox、Formation worker/sweeper、Index/TTL maintenance、Governed Context、路由记忆和默认长期记忆 scopes 的有效值。
- **BREAKING**：移除上述底层行为开关的环境变量配置入口；设置旧变量不再改变运行行为，并在迁移期启动校验中明确报错，避免静默忽略。
- 保留数据库、Provider、模型、Embedding、Milvus、凭证、容量、超时和质量阈值等基础设施或策略参数的 Secret/环境配置入口；它们不承担“是否开启记忆”的职责。
- `off` 禁止召回和新 Formation，但继续运行已有 canonical 数据所需的删除、TTL、索引收敛和审计维护；`observe` 只形成可观测决策，不向业务请求注入召回结果且不写 Memory；`on` 开启召回、自动形成及完整维护闭环。
- `on` 自动启用 Governed Context 和路由级 `user_preference/stable_fact` 召回；不自动启用 `task_memory`、`artifact_reference` 或 `session_summary`。
- 全局 `on` 不覆盖 Agent Registry 的 `context.memory` 声明。Agent 级召回仍要求显式 `prefetch + scopes`，以维持数据最小授权。
- 首期仅为 OAC 的 `strategy_analysis`、`compliance_review` 配置 Agent 级 `user_preference/stable_fact` 召回；其余 UI handoff Agent 保持禁用。
- Runtime Config、Capability、Debug 和启动日志只报告模式及其脱敏有效配置，便于识别配置断层和回归。

## Capabilities

### New Capabilities
- `memory-runtime-mode`: 定义单一记忆行为模式、内部派生矩阵、旧环境变量下线、启动校验、有效配置观测和回退语义。

### Modified Capabilities
- `memory-context-governance`: 规定 `on` 模式的路由级长期记忆范围，以及全局模式不得越过 Agent Registry 显式 memory scopes。

## Impact

- OIR Core 配置模型、依赖组装、Formation/Recall/maintenance worker 生命周期和 Runtime Config。
- Governed Context 的路由 Memory Provider 启用条件与默认 scopes。
- OAC Host composition、Capability/Readiness 以及本地/测试启动文档。
- OIR `.env.example`、OAC Host 环境示例和所有直接设置旧行为变量的测试、脚本、Runbook。
- OIR Agent Registry 中两个 OAC 对话型 Agent 的 context 配置；Adapter/Core 依赖方向和 OIR 通用性保持不变。
