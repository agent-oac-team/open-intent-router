## Context

当前 `RouterService.route()` 的实际顺序是：先按权限得到 `available_agents`，再用 `_filter_agents_by_tags()` 裁剪候选 Agent，随后把裁剪后的 `candidate_ids` 传给 Evidence Provider 和 LLM。强固定问命中虽然可以通过 `route_override` 短路 LLM，但它只能在标签裁剪后的候选集里生效。

这会带来两个问题：

- 标签筛选本质是弱召回信号，却在当前实现中拥有裁剪候选的权力，可能先于固定问强命中排除正确 Agent。
- 固定问强命中属于可解释的强确定性规则，命中后不应再交给 LLM 重新判断意图。

本次设计把路由前筛选拆成“边界、确定性裁判、召回提示、模型判断”四层，确保强规则不被弱信号覆盖。

## Goals / Non-Goals

**Goals:**

- 将筛选顺序固定为：权限过滤 > 强确定性规则 > 语义/标签筛选 > LLM 判断。
- 保持权限过滤为硬边界，任何规则都不得越权扩展候选 Agent。
- 固定问 `strength=strong` 且目标 Agent 可用时，直接返回路由结果，并确保本轮不调用 LLM。
- 固定问 `strength=strong` 但目标 Agent 被权限过滤拒绝时，直接返回无权限提示，不进入 LLM 兜底判断。
- 本版本保留标签/语义筛选代码和 metadata，但不让它裁剪 Evidence Provider 或 LLM 的候选集。
- 让 RouteResponse 和 Route Log 可以解释本轮是否发生标签命中、固定问强命中、LLM 是否被短路。

**Non-Goals:**

- 不重做 Agent Registry、AccessPolicy 或固定问 YAML 配置结构。
- 不新增向量召回、Embedding、RAG 调度或多 Evidence Provider 编排。
- 不移除标签筛选代码；本版只改变它的执行效力。
- 不提供重新启用标签/语义裁剪的配置开关；后续如果需要恢复裁剪能力，应另建新版本变更。
- 不改变 `/api/v1/route`、`/route-and-invoke`、`/route-and-execute` 的外部请求/响应 schema。

## Decisions

### Decision: 权限过滤生成唯一候选边界

`_available_agent_definitions()` 继续作为第一步，只返回 enabled 且 `access_policy` 匹配请求用户的 Agent。后续 Evidence Provider、固定问强命中、标签筛选、LLM 输出和 invocation preview 都必须在这组 Agent 内运行。

替代方案是允许 Evidence Provider 返回更大的候选集再做校验。该方案会让越权候选进入中间上下文和日志，增加安全审计成本，因此不采用。

### Decision: 强固定问在权限过滤后直接短路 LLM

Evidence Provider 接收权限过滤后的 `available_agent_ids`。如果返回 `route_override` 且目标 Agent 在该集合内，Router 直接构建 `RouteResponse`，再复用 `_clarify_or_attach_invocation()`、`_finalize_assistant_message()` 和 `_after_route()`，不再调用 LLM。

如果固定问强命中存在，但目标 Agent 不在权限过滤后的集合内，Provider 不能静默丢弃这个事实；它应通过 evidence 或 metadata 标记 `route_override_denied`、`reason=permission_denied`、`target_agent_id`。Router 收到后直接返回安全响应：

- `decision.status=unsupported`
- `decision.action=unsupported`
- `context.relation=unsupported`
- `assistant_message` / `decision.message` 使用用户可见的无权限提示
- `context.metadata.permission_denied=true`

该路径同样不调用 LLM，避免模型把无权限强命中改判到其他 Agent。

替代方案是把强命中作为高权重 hint 继续交给 LLM。该方案会让确定性配置被概率模型覆盖，不符合“固定问强命中是强路由”的要求，因此不采用。

### Decision: 标签筛选只记录召回结果，不裁剪候选

`_filter_agents_by_tags()` 或等价函数仍可计算匹配项，但返回给 Evidence Provider 和 LLM 的 `candidate_ids` 应始终是权限过滤后的 `available_agent_ids`。`TagFilterResult` 需要保留：

- 实际候选 Agent：权限过滤后的全量可用 Agent。
- 标签命中的 Agent ID：仅用于 metadata。
- 命中详情：用于 Debug、Route Log 和后续召回优化。
- 状态：例如 `matched_but_not_applied`、`no_match_no_filter`、`no_available_agents`。

替代方案是彻底删除标签筛选。考虑到后续可能需要重新启用召回优化，并且当前 UI/调试面板已读取相关 metadata，本次先保留代码入口。

本次不增加 `ROUTER_TAG_FILTER_MODE` 一类配置开关。标签/语义裁剪如果后续确实需要恢复，应通过新的 OpenSpec 版本重新定义裁剪时机、开关、观测指标和回归测试。

### Decision: 弱 Evidence 只影响上下文，不缩小候选集

当固定问或 Evidence 返回弱 `intent_hint`、`candidate_agent_ids` 或证据片段时，Router 将其写入 `RouteContext.intent_hint`、`RouteContext.evidence` 和 metadata，但 LLM 的候选集合仍使用权限过滤后的全量可用 Agent。LLM 可以参考 hint，但最终输出仍必须通过候选边界校验。

替代方案是让弱 Evidence 缩小候选集。该方案和“强确定性规则优先，弱召回不覆盖强命中”的目标冲突，且容易把知识库召回错误放大为路由错误，因此不采用。

### Decision: 用测试证明 LLM 未被调用

强固定问短路需要用显式测试验证。测试中应使用会在调用时失败或记录调用次数的 LLM stub，确保强命中路径不会进入 LLM。标签不裁剪也需要测试 Evidence Provider 和 LLM 接收到的候选 ID 都等于权限过滤后的全量可用 Agent。

## Risks / Trade-offs

- [Risk] 候选 Agent 数量变多后，LLM Prompt 更长，路由成本可能升高。→ Mitigation：本版本先保证正确性，后续可引入可解释的召回优化开关、候选预算和观测指标。
- [Risk] 标签 metadata 名称变化可能影响现有测试或前端调试显示。→ Mitigation：尽量兼容保留 `tag_filter`、`available_agent_ids`、`filtered_candidate_agent_ids` 字段，但把语义改为“不裁剪后的实际候选”，新增 `tag_filter_matched_agent_ids` 表达弱命中。
- [Risk] 固定问配置指向无权限 Agent 时，用户可能期望强路由但实际不能进入能力。→ Mitigation：固定问 provider 或 Router 记录 `route_override_denied` metadata/evidence，原因标记为 `permission_denied`，并返回用户可见的无权限提示。
- [Risk] 未来重新启用标签裁剪时可能重复引入本次问题。→ Mitigation：本版本不提供裁剪开关；若要启用裁剪，必须另建新版本变更并重新定义强规则优先级。

## Migration Plan

1. 调整 `RouterService.route()`：先权限过滤并构造 `available_agent_ids`，Evidence 固定问强命中在该集合内短路，再计算标签匹配 metadata，最后调用 LLM。
2. 修改 `_filter_agents_by_tags()` 或新增只读标签匹配函数，使本版本不裁剪候选。
3. 修改固定问 Evidence Provider，使强命中但目标不可用时保留 denied 信号，而不是静默降级为普通无命中。
4. 更新强固定问、标签 metadata、无权限提示和权限边界的单元测试。
5. 更新 API/交付文档中的候选筛选顺序描述。
6. 回滚时可恢复旧的标签裁剪逻辑，但必须保留固定问强命中不调用 LLM 的测试保护。

## Open Questions

无。
