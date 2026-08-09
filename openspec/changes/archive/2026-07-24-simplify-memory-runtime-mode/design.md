## Context

OIR 当前把记忆行为拆成十余个 `BaseSettings` 字段。由于 Pydantic Settings 会自动把字段暴露为环境变量，部署者可以组合出互相矛盾的状态，例如 Formation=`enforced`、Recall=true、worker=true，但 Governed Context 仍为 `legacy`、route memory=false；当前 OAC 本地迁移 fixture 正处于这种状态，并且 9 个迁移 Agent 的 `context.memory.mode` 均为 `disabled`。

这个问题不是文档不足，而是配置模型允许表达无效状态。需要把“产品行为模式”“Host 迁移隔离”“外部基础设施参数”和“Agent 数据权限”分开：部署者只选择产品模式，代码生成有效运行策略；OAC Host 决定 shadow/rehearsal 隔离；Secret 和外部资源继续由部署环境提供；Agent Registry 继续决定单 Agent 可访问哪些 scope。

## Goals / Non-Goals

**Goals:**

- 用 `MEMORY_MODE=off|observe|on` 作为唯一记忆行为环境开关。
- 让每个模式对应唯一、可测试、可观测的 Recall/Formation/worker/context 有效策略。
- 删除底层行为字段的环境变量绑定，并拒绝旧变量，避免静默兼容产生假配置。
- 保留 OAC Decision Shadow、State Rehearsal 与 OIR Core 通用性的边界。
- 保持 Agent 级 scope 显式授权，并以 Registry 数据完成 OAC 首期两个对话 Agent 的启用。
- 提供从旧配置到单一模式的确定性迁移和回退路径。

**Non-Goals:**

- 不改变 PostgreSQL canonical memory、Revision、Tombstone 或 Milvus 派生索引的数据模型。
- 不引入 `task_memory` 跨会话续接、历史 Plan 恢复或 OAC 会话删除传播。
- 不为 UI handoff Agent 自动注入记忆，也不修改下游页面协议。
- 不把数据库 URL、Provider、模型、Embedding、Milvus URI、凭证、容量、超时或质量阈值硬编码到源码。
- 不在本 change 新增 OAC 记忆管理页面。

## Decisions

### 1. 使用模式解析器，而不是继续暴露布尔开关

`Settings` 只保留环境字段 `memory_mode: Literal["off", "observe", "on"]`。新增不可由环境直接构造的 `MemoryRuntimePolicy`，在依赖组装前由纯函数解析，所有 service、worker、Runtime Config 和 capability 消费同一个 policy 实例。

选择纯派生策略而不是“一个主开关加若干覆盖项”，因为覆盖项会重新制造组合爆炸。测试需要能够直接构造 `MemoryRuntimePolicy` fixture，但生产 `BaseSettings` 不提供低层 override 字段。

### 2. 固定三态矩阵

| 有效行为 | `off` | `observe` | `on` |
| --- | --- | --- | --- |
| Recall service 可用 | 否 | 否，不注入业务请求 | 是 |
| Formation mode | off | observe | enforced |
| Turn Outbox consumer | 开启，记录 skipped/audit | 开启 | 开启 |
| Formation worker/sweeper | 关闭 | 开启 | 开启 |
| Index/TTL maintenance | 开启 | 开启 | 开启 |
| Consolidation | 关闭 | 关闭 | 关闭，后续单独治理 |
| Governed Context 的 Memory 路径 | 关闭 | 只保留形成观测，不注入 recall | 开启 |
| Route memory scopes | 空 | 空 | `user_preference,stable_fact` |

`off` 不等于卸载存储。删除 pending、TTL、索引修复和已有 canonical operation 必须继续收敛，防止关闭功能后留下不可删除数据。若基础设施完全不存在，应用仍通过既有 storage/provider readiness 规则失败或降级，而不是用行为开关掩盖错误。

### 3. 从环境配置面删除低层行为变量

以下变量从 `.env.example`、配置字段和运行文档的部署入口移除，并由启动期 legacy-variable guard 拒绝：

- `MEMORY_ENABLED`
- `MEMORY_RECALL_ENABLED`
- `MEMORY_FORMATION_MODE`
- `MEMORY_EXECUTION_MODE`
- `MEMORY_TURN_OUTBOX_CONSUMER_ENABLED`
- `MEMORY_FORMATION_WORKER_ENABLED`
- `MEMORY_FORMATION_SWEEPER_ENABLED`
- `MEMORY_INDEX_WORKER_ENABLED`
- `MEMORY_TTL_SWEEPER_ENABLED`
- `MEMORY_CONSOLIDATION_ENABLED`
- `CONTEXT_ROUTE_MEMORY_ENABLED`
- `CONTEXT_ROUTE_MEMORY_SCOPES`

Guard 必须在启动外部连接和 worker 之前运行，错误只列变量名和迁移目标 `MEMORY_MODE`，不输出值。`CONTEXT_PIPELINE_MODE` 属于更广泛的 Context/Knowledge 能力，可继续存在，但 `MEMORY_MODE=on` 的有效策略必须保证 Memory 走 Governed Context；`legacy` 不能关闭 `on` 已承诺的 Memory 路径。

数据库、history backend、Provider、模型、Embedding、Milvus、Secret、TTL 天数、预算、阈值、lease/retry、interval 等仍是基础设施或策略参数，可继续通过环境/Secret 提供。它们不能改变三态矩阵中的启停关系。

### 4. Host 迁移隔离由 Composition Root 注入

删除通用 Core 的 `MEMORY_EXECUTION_MODE` 环境入口后，默认 Core 执行平面为 `live`。OAC Host Composition Root 根据自身 `OAC_HOST_SHADOW_MODE` 构造执行平面：

- `off` -> `live`
- `decision` -> `decision_shadow`
- `state_rehearsal` -> `state_rehearsal`

State Rehearsal 的独立 database/collection 仍由 OAC Host 基础设施配置提供并执行隔离校验。这样 OAC 迁移语义不进入 OIR Core 公共配置，其他 Host 也可通过 composition API 注入自己的执行平面，而不是恢复环境变量。

### 5. 全局模式与 Agent 权限分层

`MEMORY_MODE=on` 只让 Route 阶段访问固定的低风险长期 scopes：`user_preference` 和 `stable_fact`。Agent 阶段必须继续检查 `AgentDefinition.context.memory.mode=prefetch` 及显式 scopes；全局 on 不提供默认 Agent scopes，缺失配置返回空 context。

OAC 首期通过 Registry revision/audit 更新 `strategy_analysis` 和 `compliance_review`，为其启用 `prefetch + user_preference/stable_fact + max_items=5`。其他 7 个 Agent 保持 disabled。该映射属于 OAC Registry 数据或迁移 seed，不进入 Adapter 条件分支，更不进入 OIR Core。

### 6. Runtime 只暴露有效配置

`/api/v1/runtime/config`、OAC capability 和 Memory health 返回：`memory_mode`、有效 execution plane、Recall/Formation/worker/context 状态、policy version 和配置来源。旧的独立布尔字段可在一个兼容响应周期内保留为 derived read-only 字段，但不得再表示独立配置源。

启动日志输出一条脱敏 mode summary。任何 Runtime/Debug 输出都不得包含环境变量值、数据库 URL、credential 或候选正文。

### 7. 采用 fail-fast 迁移，不静默忽略旧变量

只要进程环境实际设置了任一退役行为变量，启动即失败。选择 fail-fast 而不是 warning，是因为容器、PM2 或 Secret 管理中残留变量很容易让操作者误判实际模式。测试代码不再通过 monkeypatch 旧环境变量构造模式，而改用 `MEMORY_MODE` 或内部 policy fixture。

## Risks / Trade-offs

- [一次性破坏现有部署环境] -> 提供旧变量扫描脚本、映射表和启动前 dry-run；本地与测试环境先清理后重启。
- [off 仍运行 maintenance 可能不符合“完全停机”直觉] -> 文档明确 off 是停止用户可见行为而非放弃数据治理，并在 capability 展示 maintenance 状态。
- [on 自动启用 route recall 影响路由输入] -> 首期只允许两种长期低风险 scope，保留预算、tenant/user isolation、当前输入优先和 trace；通过 Golden Route replay 验证无阻塞差异。
- [Agent 全部自动启用造成越权] -> 明确禁止全局默认 Agent scopes，只通过 Registry revision 更新两个批准 Agent。
- [Host shadow 与 Core policy 双源] -> Composition Root 在启动时生成唯一 immutable policy，Runtime Config 报告最终来源并测试 OAC/通用 Core 两种组装。
- [未来需要 recall-only 或 consolidation] -> 不用隐藏布尔覆盖；通过新增正式 mode 或版本化 policy 扩展，并重新走 OpenSpec。

## Migration Plan

1. 增加 `MemoryRuntimePolicy`、三态解析和 legacy-variable guard，先保持现有测试通过。
2. 将 Recall、Formation、worker lifecycle、Context provider 和 observability 改为消费 policy。
3. 更新 OAC Host Composition Root 注入 execution plane，验证 Decision Shadow/State Rehearsal 隔离不变。
4. 更新 Runtime Config/capability，加入模式一致性门禁和低层字段 derived-only 兼容投影。
5. 更新两个 OAC Agent Registry 定义及 revision/audit，验证其余 Agent 保持 disabled。
6. 清理 `.env.example`、Host env example、测试、脚本和 Runbook 中的旧行为变量，只留下 `MEMORY_MODE`。
7. 本地使用 `MEMORY_MODE=on` 回放 Formation -> Recall、Route、两个 Agent 和权限矩阵；测试环境先 `observe`，通过门禁后再 `on`。

回退时把 `MEMORY_MODE=off` 并重启。Canonical schema、已有 revisions/tombstones 与 maintenance 不回滚；不得恢复旧低层环境变量。

## Open Questions

无。三态模式、环境变量收敛、Route scopes 和 OAC 首期 Agent 范围均已确认。
