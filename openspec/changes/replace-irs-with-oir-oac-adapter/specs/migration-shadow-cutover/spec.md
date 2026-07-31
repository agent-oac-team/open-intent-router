## ADDED Requirements

### Requirement: OIR 与 knowledge_sys 必须各自拥有唯一事实边界
系统 SHALL 将 Central Route、Registry、Session、Event、Plan、Run/Result、Context 和 Memory
的 Canonical Data 归 OIR，将 Knowledge Asset、Chunk、Search、Read、Admin、ACL、索引、
缓存与 Retrieval Trace 的 Canonical Data 归 `knowledge_sys`。

#### Scenario: 两个服务独立部署
- **WHEN** OIR 与 `knowledge_sys` 使用各自配置和进程启动
- **THEN** 任一服务不通过共享应用代码、隐式 Base URL fallback 或跨库写入拥有对方事实

#### Scenario: OIR 未配置 Knowledge Provider
- **WHEN** 独立 OIR 部署没有 Knowledge Provider
- **THEN** 路由与编排保持可用，只有 Agent 的 Knowledge Requirement 按既定状态降级或阻止调用

### Requirement: Central Retirement Gate 必须证明 OIR 完整承接
系统 SHALL 在删除 IRS 中控实现前验证 OIR Central、Registry、Agent、Session、Event、
Plan、Run/Result 契约以及 OAC Route、Agent 切换/退出、Plan 确认、回调和历史读取 E2E。

#### Scenario: 承接测试存在失败
- **WHEN** 任一要求的契约或 E2E 场景未通过
- **THEN** IRS 中控代码和对应表不得删除

#### Scenario: 承接能力全部通过
- **WHEN** 所有契约与 OAC E2E 均有当前环境证据
- **THEN** 门禁进入运行态排空和回滚演练阶段

### Requirement: 切换前必须排空 IRS 存量运行态
系统 SHALL 在中控退役前停止创建 IRS 中控运行态，并完成、取消或明确终止所有活动 Plan、
在途 Agent 和待回调 Event。

#### Scenario: 排空报告仍有活动对象
- **WHEN** 盘点仍显示 pending、running、blocked 或未收口对象
- **THEN** Central Retirement Gate 失败且不得删除中控模块

#### Scenario: 运行态已排空
- **WHEN** 所有活动对象为零或有明确终止事实
- **THEN** 系统生成 Cutover Watermark、非敏感配置快照并允许执行回滚演练

### Requirement: IRS 中控退役不依赖旧入口流量发现
系统 SHALL 将 Central Retirement Gate 与旧 Knowledge 入口兼容期分离；中控退役不要求
旧 Central 入口连续 7 天零流量，也不要求穷举未知直连调用方。

#### Scenario: Central 门禁全部通过
- **WHEN** 承接、排空、Watermark、快照和回滚演练均通过
- **THEN** 系统可删除 IRS 中控实现和表，不提供 Central 兼容代理

#### Scenario: 中控已经退役
- **WHEN** 生产切换完成
- **THEN** 回滚目标不再是 IRS，中控事实继续由 OIR 独占

### Requirement: 旧 Knowledge 入口使用独立兼容期
系统 SHALL 仅在边缘保留旧 `/central-api` Knowledge 路径的凭据验证和 JWT 换发，
不得把该入口指向 OIR 或把旧凭据传入 `knowledge_sys` Core。

#### Scenario: 迁移期旧 Knowledge 请求
- **WHEN** 已登记旧调用方使用兼容入口
- **THEN** 边缘验证旧凭据、签发短时 JWT 并调用 `knowledge_sys`

#### Scenario: 兼容入口连续 7 天零流量
- **WHEN** 所有登记调用方已切换且旧 Knowledge 入口连续 7 天无流量
- **THEN** 系统删除兼容路由与凭据转换

### Requirement: 测试与生产按同轮顺序切换
系统 SHALL 先部署 `knowledge-sys-test` 并完成跨系统、Provider、清理和 Memory 不变性门禁，
再在同一迁移轮次发布生产 `knowledge-sys` 和退役 IRS 中控。

#### Scenario: 测试门禁未通过
- **WHEN** 任一测试环境验收证据失败或缺失
- **THEN** 不发布生产，也不把 IRS 中控标记为已退役

#### Scenario: 测试门禁通过
- **WHEN** `knowledge-sys-test`、OIR 与 OAC 全部门禁通过
- **THEN** 发布生产 `knowledge-sys`，停止对应旧中控服务并开始旧 Knowledge 入口观察期
