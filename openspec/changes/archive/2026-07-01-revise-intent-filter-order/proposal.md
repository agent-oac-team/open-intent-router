## Why

当前路由链路中，标签筛选发生在 Evidence 固定问匹配之前，并会真实裁剪候选 Agent。这会导致固定问强命中被标签筛选间接覆盖，也会让“强确定性规则”仍进入 LLM 判断链路，增加不可解释性和误路由风险。

本次调整要把意图筛选规则改为确定性的分层裁判：权限过滤先决定可用边界，固定问强命中在可用边界内直接强路由，标签/语义筛选只作为召回优化信号，最后才交给 LLM 判断。

## What Changes

- 明确候选筛选顺序为：权限过滤 > 强确定性规则 > 语义/标签筛选 > LLM 判断。
- 权限过滤保持硬边界：任何后续规则、Evidence、固定问或 LLM 输出都不得扩大到用户不可访问的 Agent。
- 固定问强命中定义为强路由：当固定问配置提供 `strength=strong` 且目标 Agent 通过权限过滤时，本轮直接返回路由结果，不再调用 LLM 做意图判断。
- 标签筛选降级为召回优化信号：标签命中可以记录在上下文 metadata 中，用于解释、调试或后续优化，但不得覆盖固定问强命中。
- 本版本先保留现有标签/语义筛选代码入口，但不对候选 Agent 做实际裁剪；LLM 接收到的候选集应等于权限过滤后的可用 Agent 集。
- 更新测试与文档，确保强命中短路、权限边界和标签不裁剪都有回归覆盖。

## Capabilities

### New Capabilities

- `intent-filter-order`: 定义路由前候选过滤与强确定性规则的优先级、短路行为和可观测元数据。

### Modified Capabilities

- `intent-routing`: 调整 Router 构建候选 Agent 与调用 LLM 的行为，标签筛选不再裁剪候选集。
- `evidence-provider`: 明确固定问强命中必须在权限过滤之后、LLM 之前生效，并且命中后短路 LLM。

## Impact

- 影响 `app/services/router_service.py` 中权限过滤、标签筛选、Evidence 固定问和 LLM 调用的顺序。
- 影响 `app/plugins/evidence.py` 与固定问配置的语义说明，但不要求破坏现有 YAML 配置格式。
- 影响 Router 相关测试，尤其是候选集、固定问强命中、LLM 调用次数和上下文 metadata 断言。
- 影响 `docs/App-Desc/contracts/api.md`、`docs/App-Desc/architecture/evidence-provider.md`、`docs/App-Research/designs/中控能力设计文档.md` 或交付说明中关于候选筛选顺序的描述。
