## Why

OIR 已经具备 Context Pack、Context Budget、Memory Context 和 Knowledge Context，但 Router Prompt 仍会序列化完整请求、原始 metadata 和 Debug Pack，使被预算或策略丢弃的内容仍可能绕过 Context Pack 进入模型。路由阶段与 Agent 调用阶段又使用两套上下文组装机制，导致权限、预算、冲突、复用和可观测语义无法长期一致。

本变更要把 Context Pack 从“调试附属数据”升级为 Router 和 Agent 的唯一上下文输入闸门，并建立可扩展、可回放、保持现有公开 API 兼容的统一 Context Pipeline。

## What Changes

- 新增统一 Context Pipeline，按 `route_decision` 和 `agent_execution` purpose 组装 Context Candidate、Context Pack、Context Projection 和 Context Trace。
- 将外部 `RouteRequest`、公开 `RouteContext`、模型输入 DTO 和 Debug Trace 分离；Router Prompt 只消费白名单 Context Projection，不再直接序列化完整请求或 metadata。
- 为 Context Item 增加内部用途、消费者、来源权威性、可见范围、时效、权限、去重、冲突和策略版本语义。
- 让 Context Budget 约束最终 Router/Agent Projection，包括结构化值；关键项只能提高优先级，不能突破模型硬预算。
- 补齐 referenced/recent Agent Event、会话活动 Plan、Result 摘要和 ArtifactRef 的路由上下文读取。
- 支持 Router 在决策前按确定性策略获取受控 Memory、Knowledge 和 Evidence，并允许可靠证据驱动现有 `reply + assistant_message` 契约。
- 将 Agent Runtime Context 纳入同一治理和预算链路，继续稳定输出 `memory_context`、`knowledge_context` 和 `RouteResponse.invocation.input`。
- 增加同请求检索复用、权限/过期/冲突/去重决策、降级状态和可回放 Context Trace。
- 提供 legacy/observe/enforced 或等价 rollout 模式和回滚路径，迁移期保留 `RouteContext.metadata.context_pack` 的兼容 Debug 摘要。
- 不包含 OAC/IRS 适配、自动记忆写入、知识入库/索引、Artifact Store 或新的用户 Citation 公共字段。
- 不包含破坏性公开 API 变更。

## Capabilities

### New Capabilities

- `context-pipeline-orchestration`: 定义 Context Candidate、Pack、Projection、Trace 的统一编排阶段、purpose/consumer 边界、Provider 扩展和同请求复用。
- `context-authority-governance`: 定义后端事实源优先、消费者可见范围、权限/时效治理、去重、冲突和当前输入覆盖规则。

### Modified Capabilities

- `context-pack-contract`: Context Pack 只包含实际允许进入消费者的 included items，Prompt 必须通过受控 Projection 使用 Pack，Candidate/Debug/Trace 不得成为模型输入。
- `context-budget-control`: 预算对最终 rendered Projection 和结构化值生效，`must_include` 不得突破硬预算，并为非上下文 Prompt 内容预留模型窗口。
- `context-observability`: 增加 purpose、consumer、Provider、策略版本、冲突/去重、Projection hash 和安全回放信息，同时限制无界原文持久化。
- `session-context`: 从后端权威存储重建 referenced/recent events、活动 Plan、Result 摘要和 ArtifactRef，Host/Frontend Context 只作为补充信号。
- `agent-context-contract`: Agent 执行上下文通过统一 Pipeline 重新投影、应用总预算并复用本请求可共享的 Router 检索结果，同时保持现有稳定输入字段。
- `knowledge-context-retrieval`: Router 可以按 route purpose 进行受控知识检索并用于直接回复或路由判断，检索失败保持降级语义。
- `evidence-provider-scheduling`: 所有普通 Evidence 输出必须进入统一 Context Pipeline 的权限、去重、预算和 Trace 路径，不能直接旁路进入 Prompt。

## Impact

- Backend schema/internal DTO：`app/schemas/context.py`、`app/schemas/routing.py`、Agent Runtime Context 相关内部模型。
- Backend services：`app/services/context_service.py`、`router_service.py`、`agent_context_service.py`、`invocation_service.py`、event/plan/result repository 读取接口及依赖注入。
- Prompt：`app/prompts/router_prompt.py` 和 LLM route input 改为消费 Context Projection。
- Memory/Knowledge/Evidence：复用现有 Provider 和权限能力，增加 route purpose 编排、request-scope 复用和统一 Trace，不新增自动写入或知识索引能力。
- Logs/Debug：Route Log 和 `RouteContext.metadata.context_pack` 保持兼容展示，同时新增安全的 Trace 摘要和版本信息。
- Configuration：增加 Context Pipeline rollout mode、policy/projection version 或等价配置。
- Tests：先补当前旁路问题的失败测试，再覆盖 Pipeline、治理、预算、Router、Invocation、降级、Trace 和兼容性；现有 97 个后端测试必须持续通过。
- Public API：无破坏性变更；`assistant_message`、`decision.message`、`RouteResponse.invocation.input`、`memory_context` 和 `knowledge_context` 语义保持兼容。
