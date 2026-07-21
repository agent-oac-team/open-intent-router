## 1. Formation Decision Trace 关联

- [ ] 1.1 增加数据库回归测试：仅按 request ID 查询时能够命中 source Formation Job，但 request-filtered Events 不包含只带 formation_job_id 的 Decision Event
- [ ] 1.2 增加 turn/session/run 和多 Turn Job 回归矩阵，确认 Job 级 decision 可关联且顶层 Events 仍严格遵守调用方 filters
- [ ] 1.3 在 Memory observability 中为已授权 Formation Traces 并行加载 bounded、tenant/user/job-scoped 支撑事件
- [ ] 1.4 将 request-filtered events 与 trace support events 分离传入 `_trace_view`，按 operation_id 恢复安全 decision_id 和现有 UPDATE/DELETE proposed_operation
- [ ] 1.5 验证 pending ADD 只恢复可拒绝 decision_id、不暴露可确认 proposed_operation，敏感候选继续保持脱敏和不可推断
- [ ] 1.6 增加跨 tenant/user、resolved pending、event limit 和历史 Decision Event 兼容性测试

## 2. Memory Pending 操作展示

- [ ] 2.1 将 pending decision 的可见性、可拒绝性和可确认性拆分为集中式前端投影规则
- [ ] 2.2 为 pending UPDATE/DELETE 展示确认与拒绝，并保留 DELETE 二次确认、owner identity、expected revision 和 idempotency payload
- [ ] 2.3 为具有 decision_id 的 pending ADD/其他决策只展示拒绝，不增加 ADD 确认或操作转换
- [ ] 2.4 为缺少 decision_id 的 pending decision 展示安全的关联错误和刷新入口，避免静默隐藏操作区
- [ ] 2.5 确认/拒绝成功后刷新选中 Turn Memory Trace，并保持现有 index operation 状态刷新能力
- [ ] 2.6 增加 UPDATE、DELETE、ADD、缺失关联、precondition conflict 和 Recall provider_timeout 共存的组件测试

## 3. 运行图 Memory Formation 子流程

- [ ] 3.1 扩展 Journey 视图模型，增加 bounded formation substeps 和独立 attention 状态
- [ ] 3.2 使用 selected-turn request trace、formation jobs、candidate/semantic counts 和 decisions 投影对话收集、后台投递、候选形成与决策状态
- [ ] 3.3 仅依据 accepted lifecycle decision 及其 revision/index outcome 投影长期记忆更新和检索索引状态，避免把 pending/noop/reject 关联的旧 memory refs 当成本轮写入
- [ ] 3.4 在存在 unresolved pending decision 时将人工处理子步骤标为 attention，并让父节点优先显示待处理数量
- [ ] 3.5 将 Recall provider errors 投影到“准备参考信息”节点，不污染响应后的 Formation 子流程
- [ ] 3.6 在运行图 Memory 节点实现可访问的展开/收起交互、状态图标、连接线和适合非技术受众的中文文案
- [ ] 3.7 补充 Journey 纯投影测试，覆盖 waiting、running/retry、pending attention、accepted persisted/indexed、no candidate、noop/reject、dead-letter 和历史 Turn 重投影
- [ ] 3.8 补充桌面和窄屏组件测试，确认展开子流程无横向滚动、文本重叠或焦点丢失

## 4. 验证与文档

- [ ] 4.1 更新 Memory Debug 和运行图中文文档，明确 UPDATE/DELETE 确认矩阵、ADD 不可确认、Recall 与 Formation 的阶段边界
- [ ] 4.2 运行后端目标测试及相关 memory observability/management/PostgreSQL 集成测试
- [ ] 4.3 运行前端 typecheck、单元/组件测试、production build 和 `git diff --check`
- [ ] 4.4 使用真实 mem0/PostgreSQL 配置复测 ambiguous ADD、pending UPDATE 和 pending DELETE，核对按钮、resolution、旧 memory 不误写及子流程状态
- [ ] 4.5 使用桌面和窄屏浏览器验收运行图展开交互、控制台错误和 provider_timeout 与 pending decision 共存展示
- [ ] 4.6 运行 `openspec validate fix-pending-memory-actions-and-expand-journey --strict` 并记录验收结果
