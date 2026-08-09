# OAC-OIR 本地完整联调报告

生成日期：2026-07-16

> **Superseded 历史证据（2026-07-31，issue #22）：** 本报告中的 Knowledge
> Host 接口、只读 IRS fallback、Circuit 和 rehearsal Knowledge collection
> 只证明当时的迁移状态。相关 runtime 已退役；冻结的 wire contract fixture
> 继续作为兼容基线。

## 结论

本地自动化联调在生成时通过；其结论受上述 Superseded 边界约束。

## 拓扑与数据隔离

- 非敏感配置模板：`config/oac-host.local.env.example`
- OIR PostgreSQL 目标：独立 `oir` database
- Knowledge collection：`oir_knowledge_vectors`
- Memory collection：`oir_memory_vectors`
- State Rehearsal 使用独立 database 和两个 rehearsal collections
- IRS/OAC 历史消息和运行态导入保持关闭

## 自动化证据

| 验收面 | 结果 | 证据 |
|---|---:|---|
| OAC Adapter/Contract | 80 passed | `tests/test_oac_*.py`、`tests/contract` |
| Delegated Run/Ticket/Turn | 76 passed | delegated run、execution ticket、turn 测试 |
| Memory/Outbox/Repair/Dead-letter | 81 passed | memory acceptance、index、worker、debug metrics 测试 |
| Central 聚焦回归 | passed | Central Handler 与路由 Turn 集成测试 |
| Route Golden | 10/10 | 覆盖全部 8 种 action、权限、单/多意图 |
| Knowledge Golden | 17/17 | 覆盖 Search/Grouped/Read/Assets/Chunks 与权限 |
| OAC 中控时序 | 6/6 | 新请求、Agent、Plan、Event 和重试时序 fixture |
| Coze Knowledge transport | 3/3 | Search/Grouped/Read/Access、写权限拒绝、只读 Fallback |
| OAC TypeScript | passed | `pnpm --filter @oac/client ts-check` |
| OAC 迁移约束 | passed | `OIR_ADAPTER_MIGRATION_VERIFY_OK` |
| OAC Go | passed | `go test ./apps/server/...` |

## OAC AI Sidebar 边界

AI Sidebar 的迁移验收通过 TypeScript、迁移约束脚本和 6 条可执行中控时序完成，验证可选 Ticket 原样保存/回传、Agent final Event 使用终态、8 种 action 契约可解析，以及 Agent/Plan 时序引用不丢失。本报告不声称执行了人工浏览器点选。

## 故障演练

- 重复、伪造、过期、跨用户和重放 Ticket 均被拒绝或幂等收敛。
- 迟到 Event 不重开终态 Turn；超时和孤儿 Run 可盘点并收敛。
- Memory Formation/Recall/Worker 可独立 mode-off。
- Outbox、Formation 和索引路径覆盖 retry、dead-letter、repair 和 orphan cleanup。
- 只读 Knowledge 可按策略回退；Route 仅在 `not_accepted` 证明成立时回退；所有写操作禁止自动回退。

## Shadow Runner

`shadow-replay/v1` 的本地 Runner smoke 覆盖 Route、Knowledge、Permission 和 E2E 四类，4/4 已处理、0 blocking Diff。该 smoke 只证明 Runner 和门禁可重复执行；测试环境的全量 100% replay 仍需在阶段 14 使用实际 IRS/OIR 执行端完成。
