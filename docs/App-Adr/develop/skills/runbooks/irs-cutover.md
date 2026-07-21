# IRS 冻结、排空、切换与下线 Runbook

## 冻结

在 IRS 设置 `MIGRATION_WRITE_FREEZE_ENABLED=true`。Central、Registry、Knowledge Admin 和 Session 写入返回 503；Health 与只读 Knowledge 保持可用。确认没有新的 Session/Plan/Event/Registry/Admin write 后开始排空。

## 盘点与排空

只读盘点：

```bash
IRS_DATABASE_URL=<secret-source> PYTHONPATH=. .venv/bin/python scripts/manage_irs_cutover.py \
  --mode inventory --report <drain-report.json>
```

显式终止遗留开发态运行对象：

```bash
IRS_DATABASE_URL=<secret-source> PYTHONPATH=. .venv/bin/python scripts/manage_irs_cutover.py \
  --mode terminate --confirm-terminate-irs-runtime --report <drain-report.json>
```

报告必须显示 active Plan、in-flight Agent、pending callback 均为 0，才会生成 cutover watermark。报告只保留哈希引用，不保留消息正文。

## 切换

1. 配置 Adapter 的 `OAC_HOST_CUTOVER_WATERMARK`。
2. 将 OAC Central/Knowledge 和 Coze Knowledge URL 指向 Adapter。
3. 确认 OIR 是 Registry、Knowledge、Turn/Run/Plan/Event/Memory 唯一可写事实源。
4. 执行 OAC E2E 与 Coze Knowledge transport smoke。
5. 使用 `scripts/evaluate_oac_cutover.py` 验证全部门禁。

watermark 之前的迟到 Agent Event 返回 `accepted=false`、`route_required=false`，只进入 `.data/oac_cutover_quarantine.jsonl` 脱敏审计。

## 稳定窗口与回滚

回滚前先冻结 OIR 写入并保存 OIR watermark。只读 Knowledge 可临时切回 IRS；OIR 已完成 Turn/Run/Plan/Memory 不复制回 IRS。任何提交状态未知的写请求不得重放。未完成 Delegated Run 使用 OIR timeout/termination 端口收敛。

## 下线

只有测试环境门禁证明 blocking Diff、活动运行态、未处置迟到回调和未知消费者均为 0，且 OAC/Coze 已切换，才可移除 IRS fallback、停止 IRS 和飞书同步。历史 Session/Message/Event/Plan/Result 不导入 OIR；清理前只保留契约快照、非敏感配置、drain report 和 watermark。
