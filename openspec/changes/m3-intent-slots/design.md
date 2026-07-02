## Context

`open-intent-router` 当前已经具备基础路由、Agent Registry、Evidence Provider、Plan 和 PlanExecutor 能力。`RouteAction` 已包含 `reply`、`clarify`、`open_agent`、`continue_agent`、`exit_agent`、`show_plan`、`unsupported`、`silent`，`RouteResponse` 也已经支持 `plan`、`execution_policy` 和 `next_action`。

M1 已把本地测试台改造成聊天窗口加右侧状态面板，但聊天气泡仍只能从 `decision.message` 读取。根据《中控能力设计文档》，M3 需要新增顶层 `assistant_message`，并把 `decision.message`、`next_action.message`、`AgentInvocationResult.message`、`decision.reason` 的职责收窄到各自状态区域。同时，多意图 Plan 应以 `plan` 存在为主契约，`show_plan` 只保留兼容语义。

本阶段还需要把候选 Agent 粗筛前置到 LLM 之前。权限和 enabled 过滤仍是安全边界；标签筛选只作为首版召回收窄策略，不引入向量召回、embedding 检索或额外语义粗排。M4 后续会接管 Context Pack、预算、裁剪和压缩，因此 M3 不新增上下文预算模型。

## Goals / Non-Goals

**Goals:**

- 在 `RouteResponse` 增加顶层 `assistant_message`，并让前端聊天区优先消费它。
- 让后端为 route-only、route-and-invoke、route-and-execute 生成一致的用户可见回答。
- 多意图结果以 `plan != null` 作为计划展示主信号，服务端兜底生成 Plan 时不强制把 `decision.action` 改为 `show_plan`。
- 明确 `show_plan` 仅作为兼容动作继续接受和校验。
- 在可用性过滤之后、LLM / Evidence Provider 之前加入标签候选筛选。
- 缺少目标 Agent 必填输入时返回澄清，不产生调用预览，也不进入实际调用。
- 补齐路由动作全集、继续当前 Agent、退出当前 Agent、低置信度澄清和多意图行为的测试。

**Non-Goals:**

- 不实现 M4 Context Pack、token 预算、上下文裁剪或压缩。
- 不实现 M5 Memory API、长期用户记忆或记忆写入。
- 不实现 M6 多 Evidence Provider 调度、证据预算或 RAG 平台。
- 不新增独立聊天 Agent Invoker；聊天类子智能体仍通过 Registry 中的通用 Agent 配置表达。
- 不移除 `decision.message` 或 `show_plan`，避免破坏旧客户端。
- 不引入新的外部依赖。

## Decisions

### Decision 1: `assistant_message` 是顶层纯字符串

`RouteResponse` 增加可选 `assistant_message: str | None`。后端在 Router 归一化阶段生成该字段，首版保持纯字符串，避免过早引入多段消息、富文本、引用来源或结构化展示类型。

Router normalization 是 `assistant_message` 的最终责任边界。LLM 可以返回 `assistant_message` 作为建议文案，但后端必须在归一化阶段校验、清洗和补齐最终值；当 LLM 缺失、返回空字符串或返回不适合普通聊天区展示的内容时，后端使用规则化 fallback 生成最终 `assistant_message`。后端不得通过拼接 `next_action.message`、`AgentInvocationResult.message`、`decision.reason` 或 Debug JSON 来构造普通聊天气泡。

生成优先级：

1. 如果路由模型或后端流程已经提供明确用户可见回答，使用该回答。
2. 对 `clarify` 使用澄清问题或缺失输入提示。
3. 对含 `plan` 的响应使用计划生成或确认提示，但不拼接完整步骤明细。
4. 对 `open_agent` / `continue_agent` 使用路由到目标 Agent 的简短提示。
5. 对 `unsupported` 使用不支持提示。
6. 对 `silent` 可以为空字符串或简短完成提示，前端不应从 Debug 字段拼接气泡。

替代方案是继续让前端从 `decision.message`、`next_action.message` 和 `result.message` 拼接聊天气泡。该方案会让不同阶段的内部状态混入普通聊天区，不符合 M1/M3 的字段职责收窄。

### Decision 2: 保留 `decision.message` 作为兼容字段

M3 不删除 `decision.message`。后端仍可填充它，旧客户端和测试台 fallback 可继续使用；新客户端聊天区优先读取 `assistant_message`，缺失时才回退到 `decision.message`。

`next_action.message` 只用于 Plan / Host 协作状态；`AgentInvocationResult.message` 只用于执行摘要；`decision.reason` 只用于路由解释和 Debug。

替代方案是立刻把 `decision.message` 标记为不可用并要求所有客户端迁移。该方案会破坏已有测试台、API 使用者和外部接入方的兼容性。

### Decision 3: Plan 主契约不依赖 `show_plan`

Router 兜底为多意图生成 Plan 时，应保留更准确的动作，例如 `reply`、`open_agent` 或其他非 UI 专用动作，并设置 `context.relation=multi_task`、`plan`、`execution_policy` 和必要的 `next_action`。客户端展示计划只看 `plan` 是否存在。

`show_plan` 仍保留在 `RouteAction` 中，并继续要求 `plan` 存在，用于兼容旧路由输出或旧客户端。

替代方案是继续把兜底 Plan 改写成 `show_plan`。该方案把 UI 展示动作混入核心协议，和已有 Plan 主契约设计相冲突。

### Decision 4: 标签筛选发生在 LLM 和 Evidence Provider 之前

候选流程调整为：

```text
Registry + Access Policy
  -> available_agent_ids
  -> tag filter
  -> candidate_agent_ids
  -> Evidence Provider within candidates
  -> LLM within candidates
```

首版标签来源按统一约定收敛：

- `AgentDefinition.tags`
- `AgentDefinition.capabilities`
- `AgentDefinition.trigger.keywords`
- `AgentDefinition.trigger.positive_examples`
- `AgentDefinition.metadata.intent_tags`
- `AgentDefinition.metadata.routing_tags`

实现可以先使用轻量文本匹配和规范化，不做向量召回。若没有任何标签命中，系统应使用可解释的固定降级策略，且不得扩大到用户无权访问的 Agent。

M3 首版采用固定 fallback 策略：如果权限过滤后存在可用 Agent，但标签筛选没有命中任何 Agent，则保留全部“当前用户可用 Agent”作为 LLM / Evidence Provider 候选集，并在 `context.metadata` 或 Route Log 中记录 `tag_filter=no_match_fallback_all_available`。这避免因为标签配置不完整导致系统突然大量澄清或不支持，同时仍不突破用户权限边界。后续 M8 可以基于回归样例评估该 fallback 是否过松，再考虑改为澄清策略。

替代方案是把全部可用 Agent 继续传给 LLM。该方案在 Agent 数量增长后会降低路由质量，并使 Evidence Provider 强命中更难解释。

### Decision 5: 低置信度澄清使用保守配置

M3 增加可配置的低置信度澄清阈值。默认值应尽量保守，避免轻易打断正常路由；只有当 LLM 或规则结果提供的置信度低于该阈值，且没有 Evidence override、固定问强命中或明确目标 Agent 时，Router 才返回 `clarify`。低置信触发时必须在 Route / Debug 中记录原因、原始置信度和阈值。

替代方案是硬编码阈值或不做低置信澄清。硬编码会让不同接入方难以调节；完全不做会让低质量路由直接进入 Agent 调用。

### Decision 6: 槽位澄清复用必填输入和 `next_action`

当路由目标 Agent 存在 `required_inputs`，且无法从当前输入或现有 invocation input 构造中得到这些字段时，Router 返回 `decision.action=clarify`。如果请求路径或 Plan 协作需要结构化 Host 动作，可以同时返回 `next_action.type=collect_input`，并在 `metadata` 中包含缺失字段列表。

M3 不新增复杂槽位 schema。首版只保证缺失字段可被测试、可展示、不会误触发调用。M4/M5 后续可通过 Context Pack 和 Memory 提升槽位提取来源。

替代方案是直接调用 Agent 并让 Agent 自己报错。该方案会把路由层能提前发现的问题推迟到执行层，降低用户体验和测试可解释性。

## Risks / Trade-offs

- [Risk] `assistant_message` 与 `decision.message` 在过渡期可能不一致。 -> Mitigation: 后端集中生成 `assistant_message`，测试覆盖主要动作；文档明确聊天区只读 `assistant_message`，旧字段只作兼容。
- [Risk] 标签筛选过严会漏掉可处理 Agent。 -> Mitigation: 首版无命中时 fallback 到全部用户可用 Agent，并在 Route Log / Debug 中记录 `tag_filter=no_match_fallback_all_available`。
- [Risk] 标签筛选过松会无法改善候选规模。 -> Mitigation: 先使用 tags/capabilities/trigger/metadata 的统一来源，后续根据回归样例调整匹配规则。
- [Risk] 低置信度阈值过高会频繁打断用户。 -> Mitigation: 默认阈值保守，并允许通过配置调整；低置信澄清必须记录触发原因。
- [Risk] 继续保留 `show_plan` 会让实现者误用它。 -> Mitigation: 文档和测试都以 `plan` 存在为主判断，并只把 `show_plan` 标为兼容动作。
- [Risk] `collect_input` 的结构化字段不足以表达复杂槽位。 -> Mitigation: M3 只覆盖必填输入缺失；复杂槽位来源和上下文预算留给 M4/M5。

## Migration Plan

1. 在 schema 中为 `RouteResponse` 增加可选 `assistant_message`，保持 `decision.message` 不变。
2. 增加 Router 内部 helper，统一从 decision、next_action、plan 和调用路径生成 `assistant_message`。
3. 调整多意图兜底 Plan 逻辑，不再强制把动作改写为 `show_plan`。
4. 在 Registry / Router 层加入标签候选筛选，并确保 Evidence Provider 和 LLM 只接收筛选后的候选集。
5. 增加保守的低置信度澄清阈值配置和触发记录。
6. 将必填输入缺失澄清扩展为可选 `next_action=collect_input`，并确保不会生成 invocation preview。
7. 更新 Prompt、Mock LLM 和 OpenAI-compatible 解析，使 `assistant_message` 可被生成或被后端补齐。
8. 更新前端类型和聊天气泡 helper，优先展示 `assistant_message`，缺失时回退 `decision.message`。
9. 更新 API 和测试台文档。
10. 运行后端测试和前端测试 / 构建。

Rollback 策略：由于 `assistant_message` 是可选字段，回滚时可先让前端恢复回退 `decision.message`；标签筛选可通过配置或实现开关回退到仅权限过滤；`show_plan` 兼容动作保留，不影响旧响应。

## Open Questions

None. 本 change 按《中控能力设计文档》中已确认的 M3 口径执行；M4 Context Pack、M5 Memory 和 M6 Evidence 调度另建 change。
