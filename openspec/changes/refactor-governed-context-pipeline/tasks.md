## 1. 基线与 Characterization Tests

- [ ] 1.1 运行并记录当前 `.venv/bin/python -m pytest`、Ruff lint 和 format-check 基线，确认现有 97 个后端测试及工作区已有变更状态
- [ ] 1.2 在 `tests/test_context_prompt_projection.py` 先写失败测试，证明 dropped history/evidence 正文及完整 `structured_value` 当前仍可能进入 Router Prompt
- [ ] 1.3 在 `tests/test_context_prompt_projection.py` 先写失败测试，证明未被选择的 `frontend_context`、Context Debug Pack 和 dropped metadata 不得进入目标 Prompt
- [ ] 1.4 在 `tests/test_context_budget_enforcement.py` 先写失败测试，覆盖 `must_include` 不得突破硬预算、结构化值计入预算和关键项最小表示仍超限的受控失败
- [ ] 1.5 在 `tests/test_router_context_assembly.py` 先写失败测试，覆盖 referenced Agent Event、recent Events 和无显式 `plan_id` 的 session active Plan 当前缺失
- [ ] 1.6 在 `tests/test_context_backward_compatibility.py` 固化现有 `assistant_message`、`decision.message`、`RouteResponse.invocation.input`、`memory_context`、`knowledge_context` 和 Debug metadata 公共契约

## 2. Context Contract 与 Rollout 配置

- [ ] 2.1 在 `tests/test_context_pipeline_models.py` 先写失败测试，定义 Context Candidate、Pack、Projection、Trace、purpose、consumer、authority、visibility 和版本字段的校验行为
- [ ] 2.2 增量扩展 `app/schemas/context.py` 或新增内部 schema，实现 Candidate、Projection、Trace、Provider outcome 和 request-scoped assembly session 模型
- [ ] 2.3 保持现有公开 `ContextItem`、`ContextPack` 和 `RouteContext.metadata.context_pack` 可解析，并补兼容序列化测试
- [ ] 2.4 在 `tests/test_runtime_config.py` 先写失败测试，覆盖 Context Pipeline `legacy/observe/enforced` 模式、route retrieval 默认关闭和 policy/budget/projection version 配置
- [ ] 2.5 在 `app/core/config.py`、`.env.example` 和 runtime 安全输出中增加 Context Pipeline 配置，确保不暴露密钥且默认行为保守

## 3. Provider 接口与基础 Pipeline

- [ ] 3.1 在 `tests/test_context_pipeline_orchestration.py` 先写失败测试，覆盖 Provider 只返回 Candidate、Pipeline 固定阶段、purpose/consumer 绑定和 Trace 与 Projection 隔离
- [ ] 3.2 实现 `ContextProvider`、Provider result/status 和 `ContextPipelineService` 骨架，支持异步 Provider、timeout、skip 和 error outcome
- [ ] 3.3 实现 request/current-input/current-agent/frontend-context 基础 Provider，并将 Host/Frontend Context 标记为低 authority、非权限证明
- [ ] 3.4 实现 history/result/evidence 基础 Provider，将现有 `ContextService` 候选构造迁移到 Provider 输出且不改变公开响应
- [ ] 3.5 增加单元测试，验证任何 Provider 都不能直接修改 Router Prompt、`RouteContext.metadata` 或 Agent invocation input

## 4. Authority、Visibility 与安全治理

- [ ] 4.1 在 `tests/test_context_authority_policy.py` 先写失败测试，覆盖后端 Plan/Event/Run/Result 高于 Host 断言、Host Context 仅补充非冲突信号
- [ ] 4.2 在 `tests/test_context_authority_policy.py` 先写失败测试，覆盖 Router-only、Agent-private、指定 Agent 和 Debug-only visibility
- [ ] 4.3 在 `tests/test_context_authority_policy.py` 先写失败测试，覆盖权限拒绝、Memory TTL、Knowledge source denied、删除/禁用和敏感字段脱敏发生在排序前
- [ ] 4.4 实现 authority/visibility/purpose policy、permission/freshness/redaction governance stage 和可测试的 policy outcome
- [ ] 4.5 复用现有 Agent access、Memory subject/scope、Knowledge source policy 和 redaction 能力，禁止 Host Context 扩大权限

## 5. 去重与冲突处理

- [ ] 5.1 在 `tests/test_context_dedup_conflict.py` 先写失败测试，覆盖同一 Result/Event/Artifact、当前输入/Memory 和 Evidence/Knowledge 的稳定引用去重
- [ ] 5.2 在 `tests/test_context_dedup_conflict.py` 先写失败测试，覆盖当前输入对长期偏好的 current-turn override，不更新或删除长期 Memory
- [ ] 5.3 在 `tests/test_context_dedup_conflict.py` 先写失败测试，覆盖高风险 unresolved conflict 生成 bounded Router conflict signal 而不是静默合并
- [ ] 5.4 实现 source reference/dedupe key/safe hash 去重和 conflict key/authority/freshness/fact-type 冲突策略
- [ ] 5.5 将 `duplicate_dropped`、`overridden_for_turn`、`superseded`、`unresolved_conflict` 或等价结果写入 Trace，并验证不额外占用预算

## 6. Rendered Budget 与 Projection

- [ ] 6.1 扩充 `tests/test_context_budget_enforcement.py` 的失败测试，覆盖最终 rendered JSON/text wrapper、候选 Agent 载荷、system/schema/rules 和输出预留预算
- [ ] 6.2 实现基于模型窗口预留和现有字符/token 近似换算的可用 Context 硬预算计算
- [ ] 6.3 实现文本与 `structured_value` 的白名单投影、单项截断、引用占位和最终 Projection token/character estimate
- [ ] 6.4 修改 Context 选择逻辑，使 `must_include` 只提高优先级，最低表示仍超限时返回受控 `context_budget_exhausted`
- [ ] 6.5 增加 Evidence、Memory、Knowledge、History、Result 和 Frontend Context 的来源预算及总预算组合测试
- [ ] 6.6 验证 Context Pipeline 不调用额外 LLM 做分类、选择或摘要

## 7. Event、Plan、Result 与 Artifact Provider

- [ ] 7.1 在 `tests/test_router_context_assembly.py` 先写 repository/service 失败测试，定义 `get_event`、`list_recent_events` 和 `get_active_plan(session_id)` 的内存与数据库一致行为
- [ ] 7.2 扩展 Event repository/service 的 referenced/recent Event 只读查询，并补数据库映射和有界 limit 测试
- [ ] 7.3 扩展 Plan repository/service 的 session active Plan 查询，并补 pending/running/blocked 与终态过滤测试
- [ ] 7.4 实现 Event/Plan Provider，按 request source 和 purpose 有界加载后端权威状态
- [ ] 7.5 实现 Result/Artifact Provider，仅投影状态、bounded summary、stable Result/ArtifactRef，不默认复制无界 output
- [ ] 7.6 在 `tests/test_router_context_assembly.py` 覆盖唯一 Artifact 引用、多个候选 Artifact ambiguity signal 和无引用降级

## 8. Router Projection 与 Prompt 切换

- [ ] 8.1 在 `tests/test_router_context_assembly.py` 先写失败测试，覆盖 `route_decision` Pack/Projection 的当前输入、当前 Agent、History、Event、Plan、Result、Artifact 和 Evidence 来源
- [ ] 8.2 在 `tests/test_context_prompt_projection.py` 先写失败测试，要求 Prompt Builder 不再直接序列化完整 `RouteRequest` 或 `RouteContext.metadata`
- [ ] 8.3 新增内部 Router model input/projection DTO，并让 Context Pipeline 生成白名单请求控制字段和 selected context groups
- [ ] 8.4 修改 `RouterPromptTemplate` 和 LLM client 输入，只消费 Router Projection、access-filtered candidates、response schema hint 和 routing rules
- [ ] 8.5 在 `RouterService` 和依赖注入中创建 request-scoped assembly session，接入 Context Pipeline 并继续返回兼容 `RouteContext`
- [ ] 8.6 实现 `legacy` 和 `observe` 路径；observe 记录新旧输入摘要/Projection hash 且不调用第二次 Router LLM
- [ ] 8.7 实现 `enforced` 路径，验证 dropped/raw/debug 内容不再进入 Prompt，并运行 Router/Prompt/Context Pack 相关回归

## 9. Router 阶段 Memory、Knowledge 与 Evidence

- [ ] 9.1 在 `tests/test_context_retrieval_orchestration.py` 先写失败测试，覆盖 route-stage Memory/Knowledge 默认关闭、显式 policy 启用和无额外分类 LLM
- [ ] 9.2 在 `tests/test_context_retrieval_orchestration.py` 先写失败测试，覆盖 reliable Knowledge evidence 驱动现有 `reply + assistant_message` 且不新增 Citation 字段
- [ ] 9.3 在 `tests/test_context_retrieval_orchestration.py` 先写失败测试，覆盖 no-hit、all-denied、timeout 和 Provider error 的安全降级
- [ ] 9.4 将现有 Evidence Provider 普通 snippets 接入 Context Pipeline，保留 strong fixed-question override 的权限内确定性优先级
- [ ] 9.5 实现 Router Memory/Knowledge Provider applicability 和显式 route policy，传递正确 caller/purpose/source filters
- [ ] 9.6 将 selected route evidence 继续映射到兼容 `RouteContext.evidence` 和 Trace，验证未选中 Evidence 不进入 Prompt

## 10. Agent Execution Context 统一

- [ ] 10.1 在 `tests/test_context_invocation_projection.py` 先写失败测试，覆盖 Agent-specific visibility、声明 scope/source、稳定空/timeout/error context 字段和 Agent 总预算
- [ ] 10.2 在 `tests/test_context_invocation_projection.py` 先写失败测试，覆盖 Router 候选检索的 request-scope 复用、重新授权/预算和不允许 Router-only Candidate 泄漏
- [ ] 10.3 实现 Agent execution Pack/Projection renderer，从 selected candidates 构造现有输入字段、`MemoryContext`、`KnowledgeContext` 和 citations
- [ ] 10.4 将 `AgentContextAssemblyService` 改为统一 Pipeline 的兼容 facade，移除独立总预算/检索决策但保留公开调用行为
- [ ] 10.5 将 `RouterService._clarify_or_attach_invocation()` 和 `InvocationService._with_agent_context()` 接入同一 request-scoped assembly session/兼容补齐路径
- [ ] 10.6 验证 route-only、route-and-invoke、route-and-execute 和直接 `/invoke` 的 Agent context 行为与公开 Schema 一致
- [ ] 10.7 验证等价 Provider 检索同请求最多执行一次，跨请求和跨用户/租户不复用

## 11. Trace、日志与 Debug 兼容

- [ ] 11.1 在 `tests/test_context_trace_replay.py` 先写失败测试，覆盖 pack/trace identity、purpose、consumer、Provider outcome、authority、dedupe/conflict、预算和版本字段
- [ ] 11.2 在 `tests/test_context_trace_replay.py` 先写失败测试，覆盖 bounded Projection summary/hash 和相同输入/固定 Provider 输出的确定性比较
- [ ] 11.3 在 `tests/test_context_trace_replay.py` 先写失败测试，覆盖 Route Log、Debug metadata 不保存完整 Prompt、无界原文、完整 structured values 或 secret-like metadata
- [ ] 11.4 实现 Context Trace 构建、bounded Debug summary、Projection hash 和 policy/budget/projection version 记录
- [ ] 11.5 更新 Route Log summary 和 `RouteContext.metadata.context_pack` 兼容输出，保留现有测试台读取字段并隔离模型输入
- [ ] 11.6 增加 Trace 持久化失败的降级测试，确保不阻塞正常 Route/Invoke 且产生可观测错误

## 12. 兼容、文档与验收

- [ ] 12.1 完成 `tests/test_context_backward_compatibility.py`，覆盖现有 Route、Plan、Invocation、assistant message、Evidence override、Memory/Knowledge 和错误契约无破坏性回归
- [ ] 12.2 更新 `docs/api.md`、`docs/中控能力设计文档.md` 和上下文调试文档，说明 Candidate/Pack/Projection/Trace、rollout mode、直接回复和非目标边界
- [ ] 12.3 更新 runtime/debug 状态说明和 rollback 操作，明确 rollback 不放宽 Agent/Memory/Knowledge 权限
- [ ] 12.4 运行 `.venv/bin/python -m pytest` 并确保现有与新增后端测试全部通过
- [ ] 12.5 运行 `.venv/bin/python -m ruff check .` 和 `.venv/bin/python -m ruff format --check .`
- [ ] 12.6 运行 `openspec validate refactor-governed-context-pipeline --strict` 并修复所有校验问题
- [ ] 12.7 检查 OIR 核心代码和默认 Prompt 不包含 OAC/IRS/Coze/Feishu 等宿主专有概念
- [ ] 12.8 记录 observe/enforced 验收结果、已知差异和 legacy 清理条件；旧路径删除另开后续变更
