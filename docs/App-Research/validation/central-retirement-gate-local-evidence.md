# OIR 中控承接与 IRS 退役门禁本地证据

## 结论

2026-07-31 在隔离测试数据域完成 Central Retirement Gate 验证，报告结果为 `go`，
`deletion_allowed=true`。该结论只允许后续工单开始删除 IRS 中控代码与表；本次未删除
IRS 模块、未部署、未访问生产，也不代表旧 Knowledge 入口兼容期已结束。

机器报告：

- [Central Retirement Gate](../evidence/migration/central-retirement-gate-test.json)
- [非敏感最终快照](../evidence/migration/central-retirement-snapshot-test.json)
- [IRS 本地运行态排空](../evidence/migration/irs-local-runtime-drain.json)

## 能力承接

| 能力域 | OIR 事实边界 | 可执行证据 |
| --- | --- | --- |
| Central | Routing、Canonical Turn、应用端口 | Central compat/handler tests |
| Registry | Agent Registry | Registry compat、atomic mutation、revision audit |
| Agent | Route Decision、Agent Session、UI Handoff | 八类 Legacy Action 与 entitlement E2E |
| Session | Session / Message read model | Session API 与受信 history mapping |
| Event | Agent Event、Execution Trace | Navigation 与 Agent callback |
| Plan | Plan / Plan Step | Active Plan、Confirm、并发和所有权 |
| Run | Delegated Run、Execution Ticket | start/progress/failure/maintenance |
| Result | Agent Result、Canonical Turn completion | 原子 completion、transaction、持久化 |

聚焦回归共收集 154 项，结果为 153 passed、1 skipped。唯一 skip 是可选 PostgreSQL
并发用例；SQLite/内存契约与该仓库默认 CI 缝合点均通过。

## OAC E2E 边界

以下流程均通过 Host Adapter 的公开 HTTP/应用端口测试，并在 fallback off 或 OIR 应用端口
替身下执行，不需要 IRS 中控接口：

- Route 的八类兼容 Action；
- Agent 切换与退出；
- Plan 确认及 Active Plan 读取；
- Execution Ticket 与 Agent callback 收口；
- Session History 读取与受信上下文映射。

## 排空、水位线与快照

既有本地排空工具在隔离 IRS 数据域发现 1 个活动 Plan、1 个在途 Agent 和 1 个待回调对象，
显式终止后均为 0；Cutover Watermark 为
`2026-07-16T11:33:35.148630+00:00`。报告只包含哈希引用，不保存历史消息正文。

2026-07-31 生成的最终快照只保留 OIR/契约版本标签、Registry backend、Central fallback
布尔状态、写冻结状态和八个事实源归属。输入即使包含连接串或 Token 字段，CLI 也不会把
它们写入快照。

## 恢复与核对演练

- Write Fence 验证 OIR 写冻结能阻止中控状态写入。
- State Rehearsal 验证 Turn、Run、Result、Plan、Event 可核对且主数据域零副作用。
- 退役前 Fallback Drill 曾验证提交状态未知时不向 IRS 重放；该 Drill 与
  fallback runtime 已于 2026-07-31 按 issue #22 删除。
- Delegated Run Maintenance 验证未完成 Run 可以安全 timeout 并收敛 Turn。
- 当前 Central fail-closed 测试验证不存在 IRS runtime 依赖，IRS 不作为退役后的恢复目标。

## 明确排除

- 不要求枚举未知直连调用方；机器报告固定
  `unknown_consumer_enumeration_required=false`。
- 不要求旧 Central 连续零流量；机器报告固定
  `central_zero_traffic_observation_days=0`。
- 旧 Knowledge `/central-api` 的 7 天观察属于独立迁移门禁，不影响本结论。

## 复现

按 [IRS 中控退役门禁 Runbook](../../App-Adr/develop/skills/runbooks/irs-cutover.md) 运行能力
测试、恢复演练及 `scripts/evaluate_oac_cutover.py`。输入基线位于
`tests/contract/oac_irs/migration-records/central-retirement-test-evidence.json`。
