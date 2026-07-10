## Context

当前 OIR 有两条上下文路径：

1. `ContextService.build_route_context()` 在 Router LLM 前构建 `RouteContext` 和 `metadata.context_pack`。
2. `AgentContextAssemblyService` 在目标 Agent 确定后检索 memory/knowledge，并写入 `RouteResponse.invocation.input`。

M4 Context Pack 已经能估算预算、选择和记录 Context Item，但 `RouterPromptTemplate.messages()` 仍直接序列化完整 `RouteRequest` 和 `RouteContext`。原始 history、frontend context、Debug Pack、dropped items 以及未同步截断的 `structured_value` 都可能绕过预算进入 Prompt。Agent Context 又只执行单项截断，没有统一的总预算、消费者可见性、冲突和 Trace 语义。

本变更横跨 Router、Prompt、Invocation、Memory、Knowledge、Evidence、Event、Plan、Result 和日志。公开 Route/Invocation 契约已经被测试台和未来 Host 依赖，因此设计必须优先保持兼容，并通过 TDD、observe 模式和可回滚开关逐步切换。

约束：

- 只改造 OIR，不实现 OAC/IRS 兼容或 Host Profile。
- 后端持久化的消息、Event、Plan、Run、Result 是权威事实源；请求中的 Host/Frontend Context 只是补充信号。
- 不实现自动记忆写入、知识入库/索引、Artifact Store 或新的用户 Citation 字段。
- 不增加专用 LLM 调用做上下文选择、摘要或检索分类。
- 现有 `assistant_message`、`RouteResponse.invocation.input`、`memory_context`、`knowledge_context` 和 `RouteContext.metadata.context_pack` 保持兼容。

## Goals / Non-Goals

**Goals:**

- 建立统一 Context Pipeline，支持 `route_decision` 和 `agent_execution` 两类 purpose。
- 让最终 Context Projection 成为 Router LLM 和 Agent 的唯一上下文输入。
- 明确 Candidate、Pack、Projection 和 Trace 的边界，禁止 Debug/Trace 旁路。
- 统一来源权威性、消费者可见性、权限、时效、脱敏、去重、冲突和预算顺序。
- 补齐 referenced/recent Event、会话活动 Plan、Result 摘要和 ArtifactRef 的 Router 感知。
- 允许 Router 按显式策略获取 Memory、Knowledge 和 Evidence，并支持现有 `reply + assistant_message`。
- 让 Agent Context 使用同一 Pipeline 和硬预算，并复用同请求可共享的检索结果。
- 生成安全、版本化、可比较的 Context Trace。
- 通过 legacy/observe/enforced 模式渐进迁移，不破坏公开 API。

**Non-Goals:**

- 不新增 OAC、Coze、Feishu 或其他宿主专有字段。
- 不改变 Router 动作、Plan 主契约、Registry 权限语义或 Invoker 类型。
- 不实现模型驱动的任意 Memory/Knowledge 工具调用。
- 不实现长期记忆抽取、更新、删除或后台 consolidation。
- 不实现知识源导入、Chunk 写入、Embedding 和向量索引管理。
- 不保证首版能对历史请求做完整内容级重放；首版提供比较和回放基础数据。
- 不新增结构化用户 Citation 公共契约。

## Decisions

### Decision 1: 分离 Candidate、Pack、Projection 和 Trace

内部上下文拆成四类对象：

- `ContextCandidate`：Provider 返回、尚未完成治理的候选项；只在单次 assembly 内存在。
- `ContextPack`：完成权限、时效、冲突、去重和预算后，某个 consumer 实际可使用的 included items。
- `ContextProjection`：将 Pack 渲染为 Router 或 Agent 所需的最终 DTO/字段。
- `ContextTrace`：记录 Provider、候选、决策、丢弃、降级、版本和 Projection 摘要。

现有 `ContextItem` 和 `ContextPack` Pydantic 模型优先增量扩展或由内部模型包装，避免公开 Schema 一次性膨胀。`RouteContext.metadata.context_pack` 继续输出兼容 Debug 摘要，但该摘要不再被 Prompt 消费。

替代方案：继续把 selected/dropped/debug 全部放在一个 Context Pack 中。该方案改动小，但无法从类型和调用边界上阻止 dropped/debug 内容再次进入模型。

### Decision 2: 使用单一 ContextPipelineService 编排，两类 purpose 使用策略配置

新增统一编排入口，概念接口如下：

```python
await context_pipeline.assemble(
    purpose="route_decision" | "agent_execution",
    consumer=consumer,
    request=request,
    assembly_session=session,
    agent=agent,
)
```

Pipeline 固定阶段：

1. 收集 Provider 输出。
2. 归一化 Candidate。
3. 权限、可见范围、过期和脱敏。
4. 权威性、去重和冲突处理。
5. 单项限制、排序和硬预算选择。
6. 生成 Pack。
7. 按 consumer 生成 Projection。
8. 生成 Trace 和兼容 Debug 摘要。

`route_decision` 与 `agent_execution` 共享框架，但使用不同 Provider、visibility 和 budget policy。Agent 的 memory/knowledge 声明只影响 `agent_execution`；Router 使用独立、显式、默认保守的 route context policy。

替代方案：分别重写 RouterContextService 和 AgentContextAssemblyService。该方案短期简单，但会继续形成两套预算、Trace 和 Provider 扩展机制。

### Decision 3: Provider 只产生 Candidate，不直接修改 Prompt 或 invocation input

Provider 分为：

- 请求 Provider：当前输入、附件引用、当前 Agent、Host/Frontend Context。
- Repository Provider：History、Event、Plan、Result、ArtifactRef。
- Retrieval Provider：Memory、Knowledge、Evidence。
- System Provider：必要的策略和控制事实。

Provider 声明 source、适用 purpose/consumer、输入依赖、timeout 和 failure policy。Provider 不返回最终 Prompt 字符串，也不能直接写 `RouteContext.metadata` 或 `invocation_input`。

替代方案：保留各服务直接写字段，仅增加一个最终清理步骤。该方案无法可靠识别遗漏字段，也难以证明所有来源都被治理。

### Decision 4: 后端状态权威，Host Context 使用较低 authority

内部 Candidate 增加或派生以下通用语义：

- `authority`：例如 `authoritative`、`derived`、`host_asserted`、`model_generated`。
- `visibility`：Router、Invocation、指定 Agent、Debug/Replay。
- `source_ref`、`dedupe_key`、`conflict_key`。
- `confidence`、`freshness`、`expires_at`。
- 权限、脱敏和 policy outcome。

默认规则：

1. 权限和 policy 决定是硬边界。
2. Repository 中 Plan/Event/Run/Result 状态高于请求中的同名断言。
3. 当前用户显式输入控制当前轮，高于历史偏好和长期 Memory。
4. Host/Frontend Context 可补充页面或交互信号，但不能证明权限或覆盖后端状态。
5. 模型生成内容不能单独证明用户长期事实。

替代方案：按 source 设一个全局静态优先级。该方案无法表达“当前用户输入只覆盖当前轮，但不改写长期事实”等类型相关规则。

### Decision 5: 冲突和去重在预算前执行

去重优先使用稳定 source reference、result/event/artifact ID 和 dedupe key；只有缺少稳定引用时才使用受控内容 hash。冲突按 `conflict_key + authority + freshness + fact type` 处理。

处理结果包括：

- `duplicate_dropped`
- `overridden_for_turn`
- `superseded`
- `unresolved_conflict`

当前输入与长期 Memory 冲突时，Memory 可从当前 Projection 中被覆盖或降权，但不触发 Memory 写入。高风险或无法判断的冲突保留 Trace，并可向 Router 暴露一个受控 conflict signal 以支持 clarify。

替代方案：把所有冲突文本一起交给 LLM。该方案消耗预算、行为不确定，也可能让低权威内容覆盖后端状态。

### Decision 6: 最终 Projection 才是预算验收对象

预算不再只统计 Candidate content。Projection renderer 必须对真正发送的文本和结构化 JSON 估算字符/token，包括 `structured_value` 展开后的成本。

可用 Context Budget 由以下部分共同决定：

```text
模型窗口
- system prompt
- response schema/rules
- candidate Agent payload
- 预留输出预算
= context 可用硬预算
```

现有近似字符/token 换算继续使用，后续 Provider 返回真实 usage 时只用于观测和校准。`must_include` 只改变优先级；如果所有关键项的最小表示仍超出硬预算，Pipeline 返回受控 `context_budget_exhausted`，不得静默突破上限。

首版只使用确定性截断、摘要占位和引用化，不调用额外 LLM 压缩。

替代方案：继续按 Candidate content 估算。该方案无法覆盖结构化字段、渲染包装和重复序列化带来的真实成本。

### Decision 7: Router 使用内部 RouterModelInput 白名单投影

`LLMRouteInput` 可增量扩展内部字段或由新的内部 DTO 替代，但 Prompt Builder 不再执行：

```python
payload.request.model_dump()
payload.context.model_dump()
```

Router Projection 只包含：

- 必要请求身份和 source 控制字段。
- 当前用户输入的受控表示。
- access-filtered Candidate Agents。
- Router Context Pack 的 selected items/渲染分组。
- 现有 response schema hint 和 routing rules。

公开 `RouteContext` 继续用于响应和兼容 Debug，不作为模型事实源。

替代方案：在现有完整序列化后删除敏感 key。黑名单容易漏掉新字段，且不能证明预算项与最终 Prompt 一致。

### Decision 8: Router 阶段检索由显式 policy 启用，默认保守

Router Context Policy 决定 route-stage Memory、Knowledge 和 Evidence Provider 是否适用：

- Existing Evidence Provider 保持现有 deterministic override 语义。
- 普通 Evidence 必须作为 Candidate 进入 Pipeline。
- Router Memory/Knowledge 默认关闭或 observe，只有显式配置的 scope/source/purpose 才启用。
- Applicability 使用确定性规则、Provider 配置和请求 source，不新增分类 LLM。
- Knowledge 命中可靠证据时，Router 可返回现有 `reply + assistant_message`；依据继续放在 `context.evidence` 和 Trace。

替代方案：默认每轮检索全部 Memory/Knowledge。该方案会立即改变延迟、成本和路由行为，并扩大权限风险。

### Decision 9: 使用 request-scoped ContextAssemblySession 复用检索，不使用跨请求全局缓存

一次 Route/Invoke 流程创建 `ContextAssemblySession`，保存：

- Provider 候选结果。
- 检索签名和状态。
- source/policy/permission metadata。
- Trace 关联标识。

Router 阶段获得的候选可以在 Agent 阶段重新应用 consumer visibility 和 Agent policy 后复用。复用键至少包含 user、tenant、subject、purpose/source、query、scope/source IDs 和 policy version。

跨请求缓存不在本变更中实现，避免陈旧、撤权和跨主体泄漏。直接 `/invoke` 创建独立 assembly session。

替代方案：把 Router 最终 Pack 原样传给 Agent。Router visibility 不等于 Agent visibility，可能泄漏 Router-only 状态，也无法应用 Agent 声明。

### Decision 10: Event、Plan 和 Result repository 增加只读查询契约

Router 当前没有读取 referenced/recent Event 的依赖，Plan 只在显式 `plan_id` 时加载。Repository/Service 增加最小通用查询：

- `get_event(event_id)` 或等价方法。
- `list_recent_events(session_id, limit)`。
- `get_active_plan(session_id)`。
- 继续复用 `list_recent_results(session_id, limit)`。

Result Provider 生成摘要、状态、ArtifactRef 和引用，不默认复制无界 output。Artifact 解析首版只基于现有 Result/ArtifactRef；多候选或无稳定引用时由 Router clarify，不新增 Artifact Store。

替代方案：要求 Host 每次把完整 Event/Plan 放进 frontend_context。该方案违反后端事实源优先，也会重复暴露无界状态。

### Decision 11: Invocation 保持稳定字段，但从 Agent Pack 投影生成

`RouteResponse.invocation.input` 继续是目标 Agent 输入预览。Agent Projection renderer 负责：

- 构造现有 text/query/title 等输入。
- 从 selected Memory candidates 构建 `MemoryContext`。
- 从 selected Knowledge candidates 构建 `KnowledgeContext` 和 citations。
- 应用 Agent Definition context 声明和总预算。
- 在空、超时、错误或拒绝时保留稳定 context key/status。

`InvocationService._with_agent_context()` 保留为直接 invoke 和兼容补齐入口，但内部调用统一 Pipeline，不再维护第二套 retrieval/truncation 逻辑。

替代方案：让 Router 继续先写 invocation input，InvocationService 再独立补齐。该方案会保留重复检索和两套预算逻辑。

### Decision 12: Trace 使用 bounded summary，不新增首版数据库表

首版利用现有 Route Log `parsed_output` 和 `RouteContext.metadata.context_pack` 保存受控摘要，包含：

- pack/trace/request/session/purpose/consumer 标识。
- Provider 调用、跳过、超时和错误。
- item source references、选择状态和原因。
- authority、permission、freshness、dedupe/conflict outcome。
- budget usage。
- policy/budget/projection version。
- bounded Projection summary/hash。

不默认保存完整原文、完整 Prompt 或完整 structured values。完整持久化 replay 平台和独立 trace table 留到后续 change。

替代方案：保存完整 Context Candidate 和 Prompt。排查方便，但会带来隐私、存储和数据生命周期风险。

### Decision 13: 使用 legacy/observe/enforced 三阶段 rollout

- `legacy`：维持旧 Prompt 路径，用于紧急回滚。
- `observe`：构建新 Pack/Projection/Trace，记录差异，但 Router 仍使用旧 Prompt；不进行第二次 LLM 调用。
- `enforced`：Router 和 Invocation 只使用新 Projection。

新增模式通过配置控制，并在安全的 runtime/debug 状态中可见。回滚只切回旧输入路径，不得放宽权限过滤和 Knowledge/Memory source policy。

替代方案：一次性删除旧路径。当前 Prompt 行为和测试覆盖不足以支持无观察期切换。

### Decision 14: TDD 先覆盖旁路，再实现抽象

首批测试必须先证明当前缺陷：dropped history/evidence、frontend_context、Debug Pack 和完整 structured values 仍出现在 Prompt；`must_include` 可超预算；Event/active Plan 未被 Router 读取。

实现按最小垂直切片推进：

1. Candidate/Pack/Projection/Trace 单测。
2. Prompt Projection 负向测试。
3. Authority、权限、冲突和预算测试。
4. Event/Plan/Result Provider 集成。
5. Router retrieval 和 direct reply。
6. Invocation 统一与检索复用。
7. Trace、rollout 和全量回归。

替代方案：先重构全部服务再补测试。该方案无法区分预期行为变化和意外回归，不符合本变更的安全要求。

## Risks / Trade-offs

- [Risk] Prompt 结构变化导致路由质量漂移。 -> Mitigation: characterization tests、observe 模式、固定 Mock/LLM payload 测试和 legacy 回滚。
- [Risk] 内部模型过度抽象，开发量失控。 -> Mitigation: 只覆盖现有真实来源和两类 purpose，不引入宿主专有概念或完整工作流引擎。
- [Risk] 预算估算仍非精确 tokenizer 结果。 -> Mitigation: 使用保守换算、输出预留和硬上限；记录 provider usage 用于后续校准。
- [Risk] 当前输入过长导致关键项无法全部保留。 -> Mitigation: 单项上限、确定性截断/引用化；最小表示仍超限时返回受控错误。
- [Risk] Route-stage retrieval 增加延迟。 -> Mitigation: 默认关闭或 observe、applicability policy、并发调用、request-scope 复用和 timeout 降级。
- [Risk] Authority/conflict 规则错误覆盖有效事实。 -> Mitigation: 当前输入只做 turn override，不写长期状态；不确定冲突保留 Trace 并交给 clarify。
- [Risk] Trace 泄露敏感原文。 -> Mitigation: 权限/脱敏先于 Trace，持久化只存 bounded summary、source refs 和 hash。
- [Risk] Legacy 路径长期残留。 -> Mitigation: 配置默认值和删除条件写入 tasks，enforced 稳定一个版本周期后单独清理。
- [Risk] Provider 复用造成跨消费者越权。 -> Mitigation: 只复用 Candidate retrieval，Agent 阶段必须重新执行 visibility、Agent policy 和预算。
- [Risk] Event/Plan repository 查询扩大数据库负载。 -> Mitigation: 有界 limit、按 session/index 查询、仅在适用 source/purpose 下读取。

## Migration Plan

1. 创建 change-local characterization tests，冻结公开 Route/Invocation 契约并暴露现有 Prompt 旁路。
2. 增加内部 Candidate/Pack/Projection/Trace 和 ContextPipelineService，不接入生产路径。
3. 增加 authority、visibility、permission、freshness、dedupe/conflict 和 rendered budget 策略。
4. 增加 Event/Plan/Result/Artifact、Memory/Knowledge/Evidence Provider 和 request-scoped assembly session。
5. 在 `observe` 模式接入 Router，记录新旧输入摘要和 Projection hash，不增加第二次 LLM 调用。
6. 将 Prompt Builder 切换为 Router Projection，并运行 Router、Prompt、Context Pack 全量回归。
7. 将 AgentContextAssemblyService/InvocationService 切换到统一 Pipeline，保留稳定输入字段。
8. 在本地和测试环境切换 `enforced`，运行 pytest、ruff、OpenSpec strict validation 和必要 smoke。
9. 保留 `legacy` 一个兼容周期；满足全量门禁后再单独移除旧 Prompt 拼接和重复 retrieval 代码。

Rollback：将 pipeline mode 切回 `legacy`，恢复旧 Prompt/Invocation 输入路径；保留新 Trace 代码但不让它参与模型输入。权限过滤、Agent access policy、Knowledge source policy 和 Memory subject isolation 不随 rollback 放宽。

## Open Questions

None. Route-stage Memory/Knowledge 默认保守关闭、公开 Schema 不破坏、Citation/自动写入/OAC 适配不在本变更范围，均已在需求分析中确认。
