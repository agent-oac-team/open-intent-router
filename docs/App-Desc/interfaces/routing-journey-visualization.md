# 中控运行图说明

## 1. 定位

中控运行图位于前端测试台右侧状态面板的首个“运行图”标签，用于向开发人员、验收人员和非技术管理人员解释一次用户请求如何经过 OIR 中控。

该视图使用通俗中文展示主要阶段，不替代 Route、Plan、Context、Memory、Knowledge、Evidence 和 Debug 等技术标签。完整请求、响应和内部标识仍应在对应技术标签中查看。

## 2. 主链路

运行图按以下稳定顺序展示选中对话轮次：

1. 收到问题。
2. 准备参考信息，包括本轮实际使用的历史记忆和知识资料。
3. 中控理解判断。
4. 交给业务助手，或者协调 Plan 中的多个业务步骤。
5. 助手处理问题。
6. 返回处理结果。
7. 沉淀本次记忆。

主节点使用“等待、处理中、待处理、已完成、未使用、未完成”六种状态。点击有详情的节点，可以查看该节点的有限技术信息；运行图默认不展示原始 JSON、完整长标识符、provider 配置或 collection 信息。

“沉淀本次记忆”保留为一个主节点，并可展开以下形成过程：

1. 收集本轮对话。
2. 进入后台队列。
3. 提取记忆候选。
4. 语义校验与形成决策。
5. 等待人工处理，仅在存在 unresolved pending decision 时显示“待处理”。
6. 更新长期记忆。
7. 更新检索索引。

“待处理”表示自动流程已经给出需要人工确认的决定，不等同于系统仍在运行。主节点会优先显示待处理数量。

## 3. 数据来源

运行图只消费当前选中 `ConversationTurn` 已保存的数据：

- `RouteResponse`：中控判断、目标 Agent、候选 Agent 和 Plan。
- `InvocationResult`：Agent 执行状态和结果摘要。
- 本轮 Memory/Knowledge Context：实际进入本轮上下文的记忆、知识和引用数量。
- 本轮 Memory Request Trace：响应返回后的异步记忆形成、入库和索引状态。
- Agent Registry：将技术 `agent_id` 映射为便于展示的 Agent 名称。

全局 Memory/Knowledge 调试管理列表不参与本轮运行图计算，避免把仓库中的其他记录误报为当前请求实际使用的数据。选择历史对话轮次时，运行图会完全根据该历史 turn 重新计算。

## 4. 真实性边界

当前 `/api/v1/route-and-invoke` 是一次性 HTTP 请求。请求返回前，前端无法准确知道后端此刻正在组装 Context、执行 Router，还是调用 Agent。

因此“中控处理中”只表示整次请求仍在进行，不表示某个内部模块正在被实时追踪。此时运行图只确认“收到问题”，其他内部节点保持等待，不使用定时器依次模拟进度。

请求返回后，运行图才根据真实的 RouteResponse、InvocationResult 和本轮 Context 数据还原已经完成、跳过或失败的内部阶段。Memory Formation 属于响应后的异步链路，可以继续使用现有 Memory Request Trace 近实时更新。

运行图只根据 accepted ADD、UPDATE 或 DELETE lifecycle decision 判断本轮是否更新长期记忆。pending、NOOP 或 REJECT 即使关联了已有 `memory_id`、`revision_id` 或 ready index，也不代表本轮候选已经写入；相关写入和索引子步骤会保持等待或跳过。

`memory_context.errors` 中的 `provider_timeout` 属于请求前的 Recall 阶段，显示在“准备参考信息”。它不会被解释为响应后的 Formation 失败，也不会覆盖 Formation 的人工待处理状态。

该实现不新增统一追踪契约、SSE、WebSocket、trace 数据表或后端运行时依赖。

## 5. 结果分支

- `open_agent` / `continue_agent`：显示实际目标 Agent 名称和调用结果。
- `reply`：显示中控直接回答，Agent 交接和执行标记为“未使用”。
- `clarify`：显示需要补充信息，不能暗示 Agent 已执行。
- `unsupported`：显示当前没有匹配的处理能力，不能将合法跳过标记为系统失败。
- route-only：展示真实路由结果，Agent 执行标记为“未使用”。
- Plan / 多步骤：展示返回 Plan 中的实际步骤、Agent 和状态，不推测尚未返回的执行进度。

## 6. 响应式与可访问性

桌面端使用适合右侧栏扫描的纵向链路。工作区在窄屏下堆叠后，节点继续使用单列布局，长 Agent 名称和摘要会被约束，完整值保留在节点详情中。

可操作节点支持键盘聚焦和激活。详情窗口支持关闭按钮、Escape 和受控的背景关闭行为，关闭后焦点返回原节点。系统启用减少动态效果偏好时，处理中动画会被停用。
