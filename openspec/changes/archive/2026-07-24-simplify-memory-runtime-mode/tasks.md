## 1. 基线与配置契约

- [x] 1.1 记录当前 `Settings`、依赖组装、lifespan worker、Context Provider、OAC Host Composition、Runtime Config 和文档中全部 Memory 行为变量引用
- [x] 1.2 增加 `off/observe/on` 三态有效策略的参数化失败测试，覆盖 Recall、Formation、Outbox、Formation worker/sweeper、Index/TTL、Consolidation、Context 和 scopes
- [x] 1.3 增加旧行为环境变量残留时启动 fail-fast 的测试，断言只报告变量名和 `MEMORY_MODE`，不输出变量值或其他 Secret
- [x] 1.4 固化现有 Formation、Recall、Decision Shadow、State Rehearsal、删除、TTL、index repair 和 Agent scope 隔离回归基线

## 2. Memory Runtime Policy

- [x] 2.1 新增 `MemoryMode=off|observe|on` 和不可由环境直接构造的 immutable `MemoryRuntimePolicy`
- [x] 2.2 实现纯模式解析器及版本字段，按规格生成三态唯一有效矩阵
- [x] 2.3 从 `BaseSettings` 删除 Recall、Formation、execution、Outbox、worker/sweeper、maintenance、consolidation 和 route-memory 低层行为字段的环境绑定
- [x] 2.4 实现启动前 legacy-variable guard，覆盖全部退役变量、空值变量、混合新旧配置和无泄漏错误
- [x] 2.5 保留数据库、Provider、模型、Embedding、Milvus、凭证、预算、TTL、阈值、lease/retry/interval 等非启停参数，并验证其不能覆盖模式矩阵
- [x] 2.6 为测试和 Host Composition 提供显式 policy 构造端口，禁止恢复生产环境低层 override

## 3. Recall、Formation 与 Worker 接线

- [x] 3.1 将 `MemoryService`、Formation processor/consumer、lifecycle 和 maintenance 依赖改为消费同一个 `MemoryRuntimePolicy`
- [x] 3.2 重构 FastAPI lifespan，使 Formation worker/sweeper、Outbox consumer、Index worker、TTL sweeper 和 Consolidation 严格按 policy 启停
- [x] 3.3 验证 `off` 不召回、不新建 Formation job，但已提交 delete/TTL/index/audit operation 继续收敛
- [x] 3.4 验证 `observe` 生成 bounded candidate/decision/trace，但不产生 Item、Revision、provider index 或业务请求 Recall 注入
- [x] 3.5 验证 `on` 完成 Turn -> Outbox -> Formation -> Item/Revision -> index ready -> 后续 Recall 闭环
- [x] 3.6 验证服务重启、重复 Outbox、lease 恢复和 mode 切换不会重复写 Memory 或遗留孤儿 operation

## 4. Host Execution Plane 与通用性

- [x] 4.1 让通用 OIR Composition 默认注入 `live` execution plane，不再读取 `MEMORY_EXECUTION_MODE`
- [x] 4.2 让 OAC Host Composition 将 `OAC_HOST_SHADOW_MODE=off|decision|state_rehearsal` 映射为 `live|decision_shadow|state_rehearsal`
- [x] 4.3 保持 State Rehearsal database/collections 隔离校验，并验证误指主数据域时在 worker 启动前失败
- [x] 4.4 回放 Decision Shadow，证明即使 `MEMORY_MODE=on` 也不会注入 Recall、写 canonical Memory 或调用 provider side effect
- [x] 4.5 扩展 Adapter/Core 边界测试，确认 OAC Agent ID、Host shadow 词汇和 OAC 配置未进入通用 Core schema、prompt 或默认 policy

## 5. Governed Context 与 Agent Scope

- [x] 5.1 将 route Memory Provider 的有效启用条件改为 `MEMORY_MODE=on`，并固定 scopes 为 `user_preference,stable_fact`
- [x] 5.2 确保 `on` 的 Memory 路径使用 Governed Context；`CONTEXT_PIPELINE_MODE=legacy` 不得形成关闭 Memory 的第二个配置源
- [x] 5.3 验证 `off/observe` 不向 Router prompt、RouteResponse 或 Agent input 注入 recalled Memory
- [x] 5.4 将 Agent 缺少 scopes 的行为收敛为空 context，删除 conservative implicit defaults
- [x] 5.5 验证全局 `on` 不覆盖 `AgentDefinition.context.memory.mode/scopes`，并覆盖 tenant/user/subject/TTL/budget/current-input policy
- [x] 5.6 保持 `task_memory`、`artifact_reference`、`session_summary` 不进入首期 route defaults，并增加负向测试

## 6. OAC Registry 首期启用

- [x] 6.1 更新 OAC Registry 导入/seed 数据，使 `strategy_analysis` 和 `compliance_review` 使用 `prefetch + user_preference/stable_fact + max_items=5`
- [x] 6.2 保持其他 7 个迁移 Agent 的 Memory disabled，并增加完整 9 Agent 对账断言
- [x] 6.3 通过既有 Registry revision/audit 路径应用变更，记录真实 actor、before/after 和 revision conflict，不在 Adapter/Core 硬编码 Agent ID
- [x] 6.4 验证 OAC compat Registry 读写不会丢失或意外覆盖 native context 配置
- [x] 6.5 回放 admin/operator/user × 运营版/展业版权限矩阵，证明 Memory 配置不改变 Agent entitlement 候选边界

## 7. Runtime 观测与配置清理

- [x] 7.1 更新 `/api/v1/runtime/config` 返回 `memory_mode`、policy version、execution plane、配置来源和全部 derived effective states
- [x] 7.2 更新 OAC capability/readiness 和 Memory health/metrics，使低层状态只作为 derived read-only 投影
- [x] 7.3 增加 Runtime、Capability、日志和 Debug 脱敏测试，禁止输出环境值、数据库 URL、credential、candidate 或 Memory 正文
- [x] 7.4 从 `.env.example`、`config/oac-host.local.env.example` 和其他启动模板删除退役行为变量，只保留 `MEMORY_MODE`
- [x] 7.5 更新 Formation rollout、mode-off、State Rehearsal、Context、OAC migration 和本地启动文档，加入旧变量到三态模式的迁移表
- [x] 7.6 增加仓库扫描门禁，禁止退役变量重新进入生产配置示例或 Runbook 命令，同时允许历史 OpenSpec/归档证据保留原文

## 8. 本地与测试环境验收

- [x] 8.1 运行 Memory/Context/Host/Registry 聚焦测试、PostgreSQL integration、完整 pytest 和 Ruff check/format
- [x] 8.2 运行 OIR Web tests/build，验证 Runtime、Memory Trace、pending action 和 debug 展示适配新模式字段
- [x] 8.3 运行 OAC Go tests、migration verifier 和 Client TypeScript/build，确认 `3000 -> 8182 -> 8280` 链路无回归
- [x] 8.4 在隔离本地数据中依次启动 `off -> observe -> on`，验证副作用矩阵、两个 Agent Recall、其余 Agent disabled、删除/TTL/index maintenance 和回退
- [x] 8.5 使用真实 OAC 登录、V2 HMAC Route、Registry、Canonical Turn 和后续请求完成 Formation -> Recall 浏览器/transport 验收
- [x] 8.6 经用户单独授权后，在测试环境清理退役变量并先部署 `MEMORY_MODE=observe`，保存 mode/capability/queue/dead-letter/precision 基线
- [x] 8.7 测试环境门禁通过后经用户单独授权切换 `MEMORY_MODE=on`，执行 Route/Knowledge/Memory 100% replay 和紧急 `off` 演练
- [x] 8.8 更新 `replace-irs-with-oir-oac-adapter` 的追踪引用和验收证据，不改写其既有完成记录
- [x] 8.9 运行 `openspec validate simplify-memory-runtime-mode --strict` 并输出本地验收报告；测试环境未授权或未执行时明确保持对应任务未完成

测试环境脱敏证据见 `evidence/01-test-environment-acceptance.md`。该 change 尚未获得归档授权。
