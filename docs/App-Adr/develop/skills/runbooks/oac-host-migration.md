# OAC Host Runtime 迁移 Runbook

## 本地启动

1. 以 `config/oac-host.local.env.example` 建立不入库的本地环境配置。
2. 确认 PostgreSQL database 不是 IRS/OAC `oac`，Knowledge/Memory collection 分别为 `oir_knowledge_vectors`、`oir_memory_vectors`。
3. 启动 `uvicorn host_apps.oac.main:app`。
4. 检查 `/health` 和 `/capabilities`，确认 Write Fence enabled、双主关闭。

## Decision Shadow

运行 `PYTHONPATH=. .venv/bin/python scripts/run_oac_shadow_replay.py --report <path>`。环境验收必须替换为实际 IRS/OIR executor，并覆盖全部 Contract、Golden、Permission 和 E2E 样本。覆盖率必须为 1.0，未批准 blocking Diff 必须为 0。

## State Rehearsal

设置 `OAC_HOST_SHADOW_MODE=state_rehearsal`，并配置独立 database、Knowledge collection 和 Memory collection。任何一个值与主数据域相同都会拒绝启动。演练后对账 Turn/Run/Result/Plan/Event/Outbox/Memory，并证明主数据域对应对象数为 0。

## Circuit / Fallback

依次验证 closed、达到阈值 open、恢复窗口 half-open、探测成功 closed。只读 Knowledge 可回退；Route 必须有 `not_accepted` 证明；unknown/committed 禁止；所有 control/runtime write 禁止。

## Write Fence 与故障处置

- 双可写主源配置：启动失败。
- `write_freeze_enabled=true`：Route/control/runtime write 返回 503，Knowledge read 保持可用。
- OIR 超时且提交未知：返回 `fallback_blocked=ambiguous_commit`，使用同一 request ID 查询状态，不向 IRS 重放。
- Formation/Recall/Worker 可分别 mode-off；Outbox/Formation/Index 使用既有 dead-letter 和 repair 工具。
