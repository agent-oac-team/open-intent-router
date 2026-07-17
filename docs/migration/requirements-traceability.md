# OIR 替换 IRS 需求追踪矩阵

## 已验证 P0 能力

| 需求域 | 权威证据 | 状态 |
|---|---|---|
| Adapter 单向依赖与通用性 | `scripts/validate_host_adapter_boundaries.py`、CI gate | 通过 |
| IRS 22 接口兼容 | `tests/contract/oac_irs`、70 文件 hash index | 通过 |
| Canonical Turn/Outbox | Turn repository/service/transaction 全量测试 | 通过 |
| Delegated Run/Ticket | delegated run、ticket、late/duplicate/cross-user 测试 | 通过 |
| Registry 唯一主写与飞书停用 | Registry compat/audit/import tests、迁移报告 | 通过 |
| Knowledge canonical PostgreSQL | 269 Chunk PostgreSQL 验证、17/17 Golden | 通过 |
| Knowledge 向量重建 | `canonical-knowledge-vector-reindex.json`，无 IRS 向量复制 | 通过 |
| 空 `03` 客群槽位 | Manifest `deferred`、0 Chunk/Vector | 通过 |
| Coze Knowledge | Search/Grouped/Read/Access transport tests | 通过；不覆盖 Workflow 后处理 |
| Memory 从 completed Turn 形成 | Turn Outbox formation、幂等、mode-off、repair tests | 通过 |
| Fallback/Write Fence | operation policy、Circuit、commit proof、write block tests | 通过 |
| Decision Shadow 无副作用 | `route_decision_shadow` 及 side-effect dependency tests | 通过 |
| 迟到回调隔离 | cutover guard、脱敏 repository、Central Handler test | 通过 |
| IRS 写冻结 | IRS middleware 与 read/write tests | 通过 |
| IRS 排空工具 | 本地实际 1 个遗留 Plan 终止后 0 active report | 通过（本地） |
| 全量代码回归 | OIR 818、IRS 165、Web 18、OAC Go/TS/build | 通过 |
| OpenSpec | `openspec validate ... --strict` | 通过 |

## 尚缺测试环境权威证据

以下项目不能用本地 smoke 或静态配置替代：

- OAC Host Runtime、独立 PostgreSQL/Milvus 和 State Rehearsal 数据域已部署。
- 全量版本化 replay dataset 在实际 IRS/OIR 执行端达到 100% 覆盖。
- State Rehearsal 完整闭环且主数据域 0 副作用。
- 测试环境 Circuit/Fallback/Write Fence 演练通过。
- 测试 IRS 写冻结、排空、水位线、流量和未知消费者均通过。
- OAC Go/Next.js 与 Coze Knowledge URL 已切向 Adapter并通过 smoke。
- OIR 已成为唯一事实源，IRS fallback 已移除且服务已停用。

全部项目通过 `scripts/evaluate_oac_cutover.py` 后，才能完成最终 Definition of Done 和 IRS 下线结论。
