## ADDED Requirements

### Requirement: 100% Decision Shadow 覆盖全量版本化回放总体
系统 SHALL 在无真实用户流量时，将全部 Contract 样本、Golden Dataset、权限负向用例和合成 E2E 时序作为 Shadow 验收总体，并为每个样本同时产生 IRS/OIR 结果与 Diff。

#### Scenario: 执行全量回放
- **WHEN** 给定一个固定 replay dataset 版本
- **THEN** 每个样本均具有 IRS 对照结果、OIR Shadow 结果、版本元数据和可追溯 Diff，覆盖率为 100%

#### Scenario: 数据集增加新契约样本
- **WHEN** Adapter 新增或修改兼容契约
- **THEN** 对应样本必须进入 replay dataset，否则 Shadow 门禁不通过

### Requirement: Decision Shadow 不产生主链路持久副作用
系统 SHALL 在 Decision Shadow 中禁止外部 Agent、页面动作、Registry/Knowledge/Plan/Event/Memory 写入，并不得创建可被正常主链路召回的 Turn/Run/Result。

#### Scenario: Shadow Route 选择外部 Agent
- **WHEN** OIR Shadow 决策为 open/continue Agent
- **THEN** 系统只记录无副作决策与 Diff，不创建可执行 Ticket 且不调用外部 Agent

#### Scenario: Shadow 路径意外请求写入
- **WHEN** Decision Shadow 代码尝试调用 control/runtime write port
- **THEN** 写栅栏拒绝操作并将其标记为阻塞级 Shadow 违例

### Requirement: State Rehearsal 在独立数据域验证完整状态闭环
系统 SHALL 在独立测试 database/schema 和 Milvus collections 中执行 Canonical Turn、Delegated Run、Plan/Event、Context 和 Memory 演练，不得将演练状态写入切换后主数据域。

#### Scenario: 演练 Delegated Run 完成
- **WHEN** State Rehearsal 回放 Route、Ticket、progress 和最终 Event
- **THEN** 隔离数据域中的 Run/Result/Plan/Turn/Outbox/Memory 可完整对账，主数据域无对应活跃状态

#### Scenario: 隔离配置不完整
- **WHEN** State Rehearsal 指向主 PostgreSQL schema 或主 Milvus collection
- **THEN** 系统拒绝启动演练并报告隔离配置错误

### Requirement: Shadow Diff 区分结构差异、行为改进与阻塞回归
系统 SHALL 对 Route 比较 action/agent/relation/plan/message type/latency，对 Knowledge 比较 matched/evidence/rank/permission/warnings/latency，并为差异记录严重度、指纹、原因与批准状态。

#### Scenario: 文本措辞不同但行为一致
- **WHEN** IRS/OIR 文本不逐字相同，但 action、Agent、必要业务事实和权限一致
- **THEN** Diff 可按行为指纹标记为可接受差异，但仍保留可审计原因

#### Scenario: OIR 放宽权限或改变核心事实
- **WHEN** OIR 返回 IRS 会过滤的无权数据，或与原始文件在金额/期限/条件等核心事实上矛盾
- **THEN** Diff 必须标记为 blocking，且切换门禁失败

### Requirement: Fallback 只用于只读或可证明未提交的 Route
系统 SHALL 为每个 Adapter 操作定义 `read_only/route_stateful/control_write/runtime_write`，只允许 `read_only` 和有未提交证明的 `route_stateful` 自动回退 IRS。

#### Scenario: OIR 连接在发送前失败
- **WHEN** Circuit 已打开、连接拒绝或 DNS 失败可证明 OIR 未接收 Route/只读 Knowledge 请求
- **THEN** Adapter 可根据当前 policy 回退 IRS 并记录 fallback 审计

#### Scenario: Route 超时且提交未知
- **WHEN** OIR Route 请求超时且无法证明未创建 Turn/Plan/Run/Event/Memory
- **THEN** Adapter 禁止回退，返回可重试错误并记录 `fallback_blocked=ambiguous_commit`

### Requirement: 所有写操作受 Write Fence 保护
系统 SHALL 禁止 Registry、Knowledge Admin、Agent Event、Delegated Run Completion、Plan Action 和 Memory Write/Delete 自动回退或双写 IRS/OIR。

#### Scenario: OIR Knowledge Admin 写入失败
- **WHEN** OIR 上传、替换、重试或删除知识失败
- **THEN** Adapter 返回结构化错误，不将同一写入发送到 IRS

#### Scenario: 事实源配置出现双主
- **WHEN** 运行配置尝试同时将 IRS 与 OIR 设为同一 control/runtime 对象的可写主源
- **THEN** Host Runtime 启动失败或 readiness 为 not-ready，不接受写请求

### Requirement: 切换前必须排空 IRS 存量运行态
系统 SHALL 在切换前停止创建 IRS Session/Plan/Event 与控制面写入，盘点并完成、取消或显式终止所有活动 Plan、在途外部 Agent 和待回调 Event。

#### Scenario: 排空报告仍有活动对象
- **WHEN** 盘点显示 IRS 仍有 pending/running/blocked Plan 或未收口外部执行
- **THEN** 切换门禁失败，OIR 不得宣布成为新运行态主源

#### Scenario: IRS 已完全排空
- **WHEN** 排空报告证明活动 Plan、在途 Agent 和待回调 Event 均为零
- **THEN** 系统可生成 cutover watermark 并允许新请求从空 OIR 运行态开始

### Requirement: IRS 历史消息与运行态不迁移也不保留
系统 SHALL NOT 将 IRS/OAC 旧 Session、Message、Event、Plan、Plan Step 或 Result 导入 OIR，并 SHALL NOT 为旧历史建设展示、恢复或续跑兼容链路。

#### Scenario: 执行干净切换
- **WHEN** OIR 完成 cutover
- **THEN** OIR Turn/Run/Plan/Memory 数据域不包含 IRS 历史运行态或消息正文，新请求创建全新 canonical IDs

#### Scenario: 切换证据需要保留
- **WHEN** 需要审计 IRS 下线决策
- **THEN** 系统只保留 OpenAPI/Schema/错误样本等契约快照、非敏感配置清单、排空报告和 watermark，不保留历史消息正文与存量运行态记录

### Requirement: Cutover watermark 之前的迟到回调必须隔离
系统 SHALL 识别并隔离属于 cutover watermark 之前 IRS 运行纪元的迟到 Event，该 Event MUST NOT 创建或推进 OIR Turn/Run/Plan/Memory。

#### Scenario: 切换后收到旧 IRS Agent Event
- **WHEN** Event 关联的 request/run 时间或纪元早于 cutover watermark
- **THEN** Adapter 将脱敏元数据记录到隔离审计，不调用 Core 状态变更端口

### Requirement: IRS 下线必须通过可验证门禁
系统 SHALL 在移除 fallback 与停用 IRS 前证明 OIR 是唯一事实源、OAC/Coze 不再依赖 IRS、阻塞级 Diff 为零、所有写 fallback 被阻止、IRS 流量/活动运行态为零且迟到回调已处置。

#### Scenario: 下线门禁全部通过
- **WHEN** Contract/E2E/Golden/Permission/Shadow/Circuit/Write Fence/Drain/Coze transport smoke 报告均满足已确认阈值
- **THEN** 系统允许移除 IRS fallback 并停用 IRS

#### Scenario: 存在未知直连消费者
- **WHEN** IRS 仍观测到无法归属的活跃请求
- **THEN** 下线门禁失败，必须定位并切换该消费者后重新验证
