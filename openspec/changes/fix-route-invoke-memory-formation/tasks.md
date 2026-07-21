## 1. 回归基线与契约

- [x] 1.1 增加 `route-and-invoke` 回归测试，稳定复现 Agent Result=`completed` 但 Canonical Turn=`pending`、Outbox/Formation Trace 缺失的当前故障
- [x] 1.2 为 request -> turn -> run/result -> outbox -> formation -> memory/index/recall 定义后端高层 trace schema、终态枚举和安全 reason/error code
- [x] 1.3 为 Formation eligibility snapshot 定义严格 schema，覆盖 mode、execution mode、suppressed、reason code 和 policy version，禁止保存凭证或额外用户正文

## 2. Canonical Invocation 事务存储

- [x] 2.1 扩展 application/repository port，增加 Canonical invocation 的 `start_run` 与 `complete_run` 两阶段事务接口及内部持久化结果类型
- [x] 2.2 实现内存事务存储，以同一锁原子完成 Run 启动与 Turn 关联、终态 Run/Result/Turn/Outbox 收口
- [x] 2.3 实现 PostgreSQL Run 启动事务，使用所有权与 state version 条件更新，确保失败时不调用外部 Agent
- [x] 2.4 实现 PostgreSQL 终态事务，在同一 transaction 中更新 Run、插入 Result、完成 Turn并插入唯一 `turn.completed` Outbox
- [x] 2.5 补充数据库迁移、唯一约束和索引，使 request/run/result/turn/outbox 幂等与并发冲突可验证
- [x] 2.6 增加事务失败注入和并发测试，覆盖任一步骤失败整体回滚、重复完成幂等、旧版本冲突和跨所有者拒绝

## 3. Route 与 Invocation 收口接线

- [x] 3.1 调整 InvocationService，使 Canonical 管理请求在 invoker 调用前使用事务存储预创建 Run 并关联 Turn，外部调用保持在数据库事务之外
- [x] 3.2 调整 InvocationService 终态持久化，使用事务存储获得 result ID、完成 Turn 和 Outbox，同时保持现有 HTTP 响应契约
- [x] 3.3 将 `POST /api/v1/route-and-invoke` 接入 Canonical 两阶段收口，并覆盖 completed、failed、timeout、invalid_output 和幂等重试
- [x] 3.4 将 `route-and-execute` 的非 Plan 单 Agent 分支复用同一收口路径，避免保留第二个 pending Turn 缺口
- [x] 3.5 对已有 Canonical Turn 的调用禁用 legacy direct Turn Capture，保留 explicit `POST /invoke` 的既有 direct-invoke 语义并增加防双写测试
- [x] 3.6 验证 Formation off/observe/enforced 均不会阻止 Run/Result/Turn canonical 收口，并记录请求时 eligibility snapshot

## 4. Outbox、Formation 与 Index 闭环

- [x] 4.1 更新 Turn Outbox consumer，使其校验 completed Turn、所有权、eligibility snapshot 和当前 kill switch，并输出幂等 captured/skipped 结果
- [x] 4.2 加固 Formation Turn/Job 幂等键，确保重复 Outbox、worker lease 恢复及 legacy 数据共存时不产生重复 Memory Revision 或 Provider Write
- [x] 4.3 扩展 Formation trace 终态，区分 `completed_no_candidate`、`policy_rejected`、`retry`、`dead_letter`，保留尝试次数和安全错误历史
- [x] 4.4 将 Item/Revision 已提交但 index operation 未 ready 的状态投影为 `index_pending/retry/dead_letter`，仅在 canonical 与 mem0 index 均 ready 后报告 `persisted`
- [x] 4.5 增加 Formation Model 无效响应后重试成功、重试耗尽、policy 全拒绝、index 暂时失败后恢复和重复投递的集成测试

## 5. 滞留 Turn 对账与可观测性

- [x] 5.1 实现默认 dry-run 的 orphan Turn 扫描器，按所有权与唯一 Run/Result 关联分类 `repairable_enabled`、`repairable_skipped`、`ambiguous`、`ownership_conflict`
- [x] 5.2 实现显式范围和幂等 key 保护的修复命令：eligible 记录补完整 Turn/Outbox，suppressed/off/private 记录只完成 Turn 和 skipped 审计
- [x] 5.3 扩展 Memory Debug/Observability 查询，返回 request 到 Turn、Run/Result、Outbox、Formation、Lifecycle、Memory/Revision、Index 和 Recall 的有界关联摘要
- [x] 5.4 增加 pending Turn、Outbox lag、trace_missing、Formation dead-letter、index out-of-sync 指标和 runtime health 汇总
- [x] 5.5 为对账、调试和指标增加身份隔离与脱敏测试，确保跨 tenant/user 查询不泄露正文或记录存在性

## 6. 测试台状态反馈

- [x] 6.1 更新前端类型和 Memory Trace 状态映射，消费后端 `overall_stage/terminal/retryable/reason_code`，删除由前端猜测 `not_triggered` 的逻辑
- [x] 6.2 调整逐轮轮询为有上限退避：后端终态停止，达到主动轮询上限时保留最后状态、更新时间和手动刷新入口
- [x] 6.3 在 Memory tab 展示 Turn/Outbox/Formation/Lifecycle/Index 各阶段以及 skipped、no-candidate、rejected、retry、dead-letter、persisted 明确终态
- [x] 6.4 补充前端测试，覆盖状态不消失、trace_missing、mode-off skipped、Formation retry/dead-letter、index pending 和 persisted 关联详情

## 7. 端到端验收与恢复演练

- [x] 7.1 使用真实 PostgreSQL、确定性 Formation Model 和可验证 index adapter 建立 API E2E，用“访前准备 + 我喜欢吃猪肉”验证 preference Item/Revision/index ready
- [x] 7.2 在后续同 tenant/user 且允许 `user_preference` scope 的请求中验证能召回猪肉偏好，并核对 recall usage 与原 request/turn/memory 关联
- [x] 7.3 增加 off、observe、enforced 三模式 E2E，验证 canonical 收口恒成立且只有 enforced 产生 Memory side effect
- [x] 7.4 增加服务在 Turn 完成后、Outbox 消费前以及 index pending 时重启的恢复测试，验证最终只形成和索引一次
- [x] 7.5 增加本地 Milvus Lite + 真实 Formation Provider smoke 脚本和运行说明，在有凭证环境验证严格 JSON 重试、真实 embedding/index 和 recall 闭环
- [x] 7.6 对现有数据库先执行 orphan dry-run，保存统计与分类证据；不得在验收中自动回填历史 suppressed/off/private 请求

## 8. 验证与文档

- [x] 8.1 运行相关后端测试、PostgreSQL integration、完整 pytest、ruff lint 和 ruff format check，并修复所有回归
- [x] 8.2 运行前端 `npm run test` 和 `npm run build`，确认现有用户工作区改动被保留且状态检查器无布局或类型回归
- [x] 8.3 更新 API、Canonical Turn、Memory Formation rollout、运维对账和本地 smoke 文档，明确 off 是终态 skipped 而非可恢复 pause
- [x] 8.4 运行 `openspec validate fix-route-invoke-memory-formation --strict` 并记录最终 request -> recall 验收证据
