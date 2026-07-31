# IRS 中控退役门禁 Runbook

本 Runbook 只验证 IRS 历史中控是否可由 OIR 完整承接。它不删除 IRS 模块、不部署服务、
不访问生产，也不处理旧 Knowledge 入口的 7 天兼容期。

## 门禁边界

Central Retirement Gate 只要求：

1. Central、Registry、Agent、Session、Event、Plan、Run、Result 八个能力域有可执行契约证据。
2. OAC Route、Agent 切换/退出、Plan 确认、Agent 回调和历史读取不调用 IRS 中控而通过。
3. IRS 活动 Plan、在途 Agent 和待回调 Event 已排空或明确终止。
4. Cutover Watermark、非敏感配置快照及恢复/数据核对演练证据完整。

门禁不要求枚举未知直连调用方，也不要求旧 Central 入口连续 7 天零流量。旧 Knowledge
入口的调用方切换与观察期属于独立门禁。

## 1. 执行能力与 OAC E2E 验证

能力映射和对应 pytest 命令由
`host_adapters/oac/cutover/gate.py::CENTRAL_CAPABILITY_MAPPING` 维护。运行当前完整集合：

```bash
.venv/bin/python -m pytest \
  tests/test_oac_central_compat.py \
  tests/test_oac_central_handlers.py \
  tests/test_oac_registry_compat.py \
  tests/test_registry_atomic_mutation.py \
  tests/test_registry_revision_audit.py \
  tests/test_oac_agent_entitlement_routing_e2e.py \
  tests/test_api.py \
  tests/test_plan_confirm_concurrency.py \
  tests/test_plan_repository_ownership.py \
  tests/test_delegated_run_contracts.py \
  tests/test_delegated_run_start.py \
  tests/test_delegated_run_progress.py \
  tests/test_delegated_run_failure.py \
  tests/test_delegated_run_maintenance.py \
  tests/test_delegated_run_completion.py \
  tests/test_turn_transaction_coordinator.py \
  tests/test_canonical_invocation_store.py
```

测试失败时不得在输入证据中手工标记通过。

## 2. 盘点与排空

只读盘点：

```bash
IRS_DATABASE_URL=<secret-source> PYTHONPATH=. .venv/bin/python scripts/manage_irs_cutover.py \
  --mode inventory --report <drain-report.json>
```

仅在已确认的非生产 IRS 测试数据域中显式终止遗留运行对象：

```bash
IRS_DATABASE_URL=<secret-source> PYTHONPATH=. .venv/bin/python scripts/manage_irs_cutover.py \
  --mode terminate --confirm-terminate-irs-runtime --report <drain-report.json>
```

报告必须显示 active Plan、in-flight Agent、pending callback 均为 0，才会生成 Cutover
Watermark。报告只保留哈希引用，不保存消息正文。生产环境不使用本工单的终止命令。

## 3. 恢复与数据核对演练

> 2026-07-31 / issue #22：IRS runtime fallback 与 Fallback Drill 已退役。
> 当前恢复策略是冻结新写、核对 OIR canonical 状态并 fail closed；IRS 不再是
> 可调用的恢复目标。

演练至少覆盖：

- OIR 写冻结可阻止新的中控状态写入；
- State Rehearsal 的 Canonical Turn、Run、Result、Plan、Event 数据可核对且主数据域零副作用；
- 提交状态未知的请求不向 IRS 重放；
- 未完成 Delegated Run 可通过 timeout/termination 收敛；
- IRS 中控不作为退役后的回滚目标。

对应确定性验证：

```bash
.venv/bin/python -m pytest \
  tests/test_oac_governance.py \
  tests/test_oac_state_rehearsal_script.py \
  tests/test_delegated_run_maintenance.py \
  tests/test_oac_central_handlers.py::test_route_failure_is_fail_closed_without_irs_runtime_dependency
```

所谓“恢复”是冻结新写入、按稳定 request/turn/run/event/plan 身份核对并把未完成对象收敛；
不是把已由 OIR 接受或完成的状态复制回 IRS。

## 4. 生成非敏感快照与 go/no-go 报告

以 `tests/contract/oac_irs/migration-records/central-retirement-test-evidence.json` 为字段示例。
输入只保存版本标签、布尔配置状态、测试引用和排空报告，不得包含 URL、连接串、Token、
Key、Cookie、签名或业务正文。

```bash
PYTHONPATH=. .venv/bin/python scripts/evaluate_oac_cutover.py \
  --evidence <central-retirement-evidence.json> \
  --report <central-retirement-gate.json> \
  --snapshot-report <central-retirement-snapshot.json>
```

CLI 只把 allowlist 字段写入快照。任一必需证据缺失或失败时退出码为 `2`，报告
`decision=no-go` 且 `deletion_allowed=false`；只有全部必需项通过时退出码为 `0`，报告
`decision=go`。

`go` 只允许后续工单删除 IRS 中控代码与表，不代表允许部署、访问生产、删除 Knowledge
模块或跳过后续测试。
