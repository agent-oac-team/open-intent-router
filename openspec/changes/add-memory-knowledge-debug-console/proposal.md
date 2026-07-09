## Why

OIR 已经具备 memory、knowledge、mem0、PostgreSQL 和 Milvus Lite 的后端闭环，但前端仍主要按最新一次路由响应展示状态，难以回答“这一轮对话实际用了哪些记忆和知识”。现在需要把测试台升级成可观察的调试台，让开发和验收人员能按每轮对话追踪 memory/knowledge 使用情况，并通过只读管理页查看当前记忆、知识源、chunk 和检索日志。

## What Changes

- 将前端对话状态从松散 message 列表升级为 conversation turn 模型，每轮保存自己的 route response、invoke result、memory context 和 knowledge context。
- 在每轮对话气泡下展示本轮使用的 memory/knowledge 摘要，包括 item 数、citation 数、denied source 数和状态。
- 将右侧状态栏中的 Memory/Knowledge 展示拆成独立 tabs，支持查看选中 turn 的 memory items、knowledge items、citations、source IDs、scores 和 errors。
- 增加只读管理页或管理视图，接入现有 `/api/v1/memories/debug` 和 `/api/v1/knowledge/debug`，展示 memory items/events、knowledge sources/chunks/logs 和 runtime 配置摘要。
- 保持本 change 不实现编辑、删除、reindex 和持久化 per-turn trace；这些能力后续单独设计。

## Capabilities

### New Capabilities

- `conversation-context-trace`: Covers per-turn frontend tracking and display of memory/knowledge usage in the conversation and right-side inspector.
- `memory-knowledge-debug-console`: Covers read-only debug management views for memory and knowledge data using existing debug APIs.

### Modified Capabilities

- None.

## Impact

- Frontend: `web/src/App.tsx` conversation state, status inspector tabs, API client/types, styles, and tests.
- Backend: no required API contract changes for P0/P1 scope; existing `/api/v1/memories/debug`, `/api/v1/knowledge/debug`, route, and route-and-invoke responses are used.
- Documentation: update visual/debug UI docs to describe per-turn trace and read-only management scope.
- Tests: frontend component tests should cover per-turn selection, memory/knowledge badges, split tabs, and debug API rendering.
