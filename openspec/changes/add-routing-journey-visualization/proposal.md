## Why

OIR 前端不仅承担本地调试，还需要向行内开发人员和管理人员直观展示中控如何理解问题、选择 Agent、调用能力以及使用记忆与知识。现有右侧状态面板以 Route、Context、Memory 等技术字段为主，非技术人员难以从中快速理解一次请求经过的完整链路。

## What Changes

- 在右侧状态区域增加面向演示的“中控运行图”，用通俗中文展示收到问题、准备参考信息、中控判断、选择业务助手、助手处理、返回结果和沉淀记忆等阶段。
- 请求提交后仅将可确认的“收到问题”和综合“中控处理中”状态标记为进行中；请求返回前不得模拟路由、Agent 调用等内部模块的精确实时进度。
- 请求返回后基于当前 turn 已保存的 RouteResponse、InvocationResult、Memory/Knowledge Context 和 Memory Request Trace 还原本轮实际完成、跳过或失败的链路。
- 支持直接回答、澄清、无可用 Agent、单 Agent、Plan/多步骤等不同结果路径，并在图中显示实际目标 Agent 的友好名称。
- 点击流程节点可查看与该节点相关的简要技术信息；原有 Route、Plan、Context、Memory、Knowledge、Evidence 和 Debug 详情继续保留，默认演示视图不展示冗长标识符和原始 JSON。
- 提供清晰且一致的等待、处理中、完成、跳过和失败视觉状态，并适配桌面与窄屏布局。
- 不新增统一请求追踪契约、流式接口、SSE、WebSocket、持久化 trace 表或新的后端运行时依赖。

## Capabilities

### New Capabilities

- `routing-journey-visualization`: 定义基于现有每轮响应与 Memory Trace 数据构建中控运行图、状态映射、结果分支、详情交互及响应式展示的行为。

### Modified Capabilities

- None.

## Impact

- Frontend: `web/src/App.tsx` 中的 per-turn 状态投影、右侧状态区域与节点详情交互，`web/src/styles.css` 的流程布局和状态样式，以及相关前端测试。
- Existing contracts: 复用 `ConversationTurn`、`RouteResponse`、`InvocationResult`、Memory/Knowledge Context 和 `MemoryRequestTrace`；不要求修改后端 API schema。
- Backend and storage: 无新增接口、事件存储、数据库迁移或后台任务。
- Documentation: 更新中文前端说明，明确该视图是“请求处理中状态 + 返回后的真实链路还原”，不是内部模块级实时追踪。
