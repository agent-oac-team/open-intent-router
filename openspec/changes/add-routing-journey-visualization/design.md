## Context

当前前端已经按 `ConversationTurn` 保存每轮的 RouteResponse、InvocationResult、Memory/Knowledge Context 与 Memory Request Trace，右侧 `StatusInspector` 则通过 Route、Plan、Context、Memory、Knowledge、Evidence 和 Debug tabs 展示技术信息。这些数据足以在请求完成后还原本轮实际经过的主要链路，并能在响应返回后继续观察异步记忆形成状态。

`POST /api/v1/route-and-invoke` 仍是一次性请求。前端在请求返回前只能确认“请求正在由中控处理”，不能确认服务端此刻处于 Context、Router 还是 Invocation 阶段。本 change 的主要受众包括不熟悉 OIR 内部字段的管理人员，同时必须保持开发人员现有调试能力和展示真实性。

## Goals / Non-Goals

**Goals:**

- 在现有右侧状态区提供默认可见、通俗易懂的中控运行图。
- 用稳定的状态模型表达等待、处理中、完成、跳过和失败。
- 请求完成后仅根据本轮真实返回数据还原实际路径，不把全局 debug 数据误认为本轮使用数据。
- 展示 Memory/Knowledge 是否参与、目标 Agent、执行结果以及响应后的记忆形成进度。
- 兼容 route-only、route-and-invoke、直接回答、澄清、不支持、单 Agent 与 Plan/多步骤结果。
- 保留现有技术 tabs，并让流程节点按需打开受控的技术详情。
- 保持桌面三栏布局可扫描，并在窄屏下可靠堆叠。

**Non-Goals:**

- 不实现内部模块级实时追踪，也不把推测动画标记为真实运行状态。
- 不新增统一追踪事件、SSE、WebSocket、轮询 API、持久化 trace 表或数据库迁移。
- 不改变 Router、Invocation、Plan 或 Memory Formation 的执行顺序和业务语义。
- 不替代现有 Route、Plan、Context、Memory、Knowledge、Evidence 和 Debug 详情。
- 不把 OAC、mem0、Milvus 或特定银行业务名称固化为核心流程阶段。

## Decisions

### Decision 1: 在现有 StatusInspector 中新增默认 Journey tab

在右侧 tabs 的首位增加“运行图”，并将其作为初始选中项。运行图使用适合窄侧栏的纵向主链路；Route、Plan、Context、Memory、Knowledge、Evidence 和 Debug tabs 保持现有职责与顺序。

选择该方案是因为当前页面已经采用左侧配置、中间对话、右侧状态三栏布局。增加第四列会压缩对话区并恶化中等宽度设备体验；完全替换技术 tabs 又会削弱开发调试能力。

替代方案：在技术 tabs 上方永久放置流程图。该方案会让右栏过长，并导致流程图与当前技术内容同时争夺首屏空间，因此不采用。

### Decision 2: 使用纯前端 Journey Projection

新增无副作用的投影函数，将选中 `ConversationTurn`、Agent 列表及现有 trace 状态转换为稳定的视图模型：

```ts
type JourneyNodeState = "waiting" | "active" | "completed" | "skipped" | "failed";

type JourneyNode = {
  id: "input" | "context" | "routing" | "handoff" | "invocation" | "response" | "formation";
  label: string;
  summary: string;
  state: JourneyNodeState;
  detail?: JsonRecord;
};
```

投影只读取当前 turn 已保存的数据，不请求全局 Memory/Knowledge debug 列表。目标 Agent 名称通过当前 Agent Registry 映射，找不到名称时才使用 `agent_id` 作为回退。

替代方案：让各 React 组件分别判断 RouteResponse 字段。该方案会使状态规则散落在 UI 中，难以覆盖跳过、失败和旧 turn 选择等组合，因此不采用。

### Decision 3: 将请求中状态与完成后链路还原明确分开

提交后立即完成“收到问题”节点，并显示一个独立的“中控处理中”综合状态。请求尚未返回时，Context、Router、Agent 等下游节点保持等待，不按定时器依次高亮。

请求完成后，综合状态变为“链路已还原”，投影函数再根据真实响应更新各节点：

- RouteResponse 存在时，准备参考信息与中控判断可标记为完成。
- `decision.action` 为 `open_agent` 或 `continue_agent` 且存在目标时，交给业务助手标记为完成。
- 直接回答、澄清、不支持、静默或退出等路径将 Agent 交接与调用标记为跳过，并给出通俗原因。
- route-only 或没有 InvocationResult 的合法路径将助手执行标记为跳过，而不是失败。
- InvocationResult 根据实际 `status` 映射为完成或失败。
- assistant message 根据 turn 状态映射返回结果节点。
- Memory Trace 的 loading/pending、success、not_triggered、error 分别映射为处理中、完成、跳过、失败。

替代方案：用固定延迟轮流高亮模块。该方案视觉上更活跃，但会错误暗示服务端真实阶段，因此明确禁止。

### Decision 4: 用通俗主链路承载架构解释，用节点详情承载技术信息

默认节点文案使用“收到问题”“准备参考信息”“中控理解判断”“交给业务助手”“助手处理问题”“返回处理结果”“沉淀本次记忆”。Memory 和 Knowledge 是否参与以“历史记忆”“知识资料”等短标签显示，不展示 provider、collection、request_id、run_id 或原始 JSON。

点击节点打开复用现有 modal/dialog 交互的只读详情，展示与该节点直接相关的有限字段。完整 payload 继续留在 Debug 等技术 tabs 中，避免在运行图复制一套 JSON 调试器。

替代方案：直接把技术字段写在节点内。该方案会重现当前信息过载问题，也不适合管理人员演示，因此不采用。

### Decision 5: 以分支路径表达不同结果，不强迫所有请求经过 Agent

运行图保持统一骨架，但节点摘要和跳过原因必须反映真实决策：

- direct reply：中控直接回答，Agent 相关节点跳过。
- clarify：中控请求补充信息，Agent 相关节点跳过。
- unsupported：明确显示当前没有合适处理能力。
- single Agent：显示友好 Agent 名称和实际 Invocation 状态。
- Plan/multi-step：在“业务助手协作”节点内展示步骤数量及简化步骤列表；步骤状态来自现有 Plan 数据，不推测未返回的执行进度。

这样既保持可比较的视觉结构，也避免把跳过误解为系统故障。

### Decision 6: 使用现有 React、CSS 和 Lucide 能力实现

流程图采用语义化 DOM、CSS Grid/Flex 和伪元素连接线，图标继续使用项目现有 Lucide 组件。不引入 graph/canvas/diagram 依赖。节点尺寸、文本截断、状态色和焦点样式保持稳定；窄屏时运行图随右栏整体堆叠，不产生横向滚动。

选择该方案是因为本视图是固定、有限状态的纵向链路，不需要自由拖拽、缩放或自动图布局。减少依赖也能降低构建体积和维护成本。

## Risks / Trade-offs

- [Risk] 管理人员可能把“中控处理中”理解为精确实时追踪。 -> Mitigation: 使用综合状态文案，并在请求返回前保持内部节点等待；文档明确其数据边界。
- [Risk] 请求完成后的快速状态变化可能不易观察。 -> Mitigation: 完成态长期保留在所选 turn 中，允许选择历史轮次反复查看，不人为延迟真实结果。
- [Risk] 不同 action 和 Plan 组合会产生复杂状态映射。 -> Mitigation: 将映射集中在纯投影函数并为每类路径增加表驱动测试。
- [Risk] Memory Trace 在响应之后仍可能推进，导致选中历史 turn 时状态变化。 -> Mitigation: 继续沿用当前 per-turn polling 和版本保护机制，运行图只消费该 turn 的最新 trace state。
- [Risk] 右侧 320-430px 宽度可能导致长 Agent 名称溢出。 -> Mitigation: 节点主文案限制行数，标识符使用单行省略，完整值仅在详情 dialog 中展示。
- [Trade-off] 不新增后端追踪意味着 Router 与 Invocation 的进行中阶段无法分别点亮。 -> Mitigation: 接受该限制，以更低复杂度满足架构展示目标；未来若需求转为工程级实时诊断，再单独提案。

## Migration Plan

1. 增加 Journey 投影类型、状态映射函数及单元测试，不改变现有请求流程。
2. 在 StatusInspector 中增加默认 Journey tab 和空态、请求中、完成态展示。
3. 增加节点详情 dialog、Agent 友好名称映射与 Plan 简化步骤展示。
4. 完善响应式样式、键盘可访问性和前端回归测试。
5. 更新中文文档，并运行前端 test/build 与 OpenSpec strict validation。

回滚仅需移除 Journey tab、投影函数和样式；现有 API、ConversationTurn 数据和技术 tabs 不受影响。

## Open Questions

- 暂无阻塞实现的问题。首版固定使用纵向布局；是否增加独立“演示模式”全屏入口，可根据实际展示反馈另行评估。
