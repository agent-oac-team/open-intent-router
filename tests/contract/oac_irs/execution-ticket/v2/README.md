# Execution Ticket v2

v2 在 v1 不透明 Ticket 契约上增加 Plan Step 条件完成语义。旧客户端仍可不传新增字段，并继续遵循 v1 的唯一受信关联规则。

新 OAC 客户端完成 Plan Step 时：

- `event_id` 由 `plan_id + step_id + expected_state_version` 稳定生成；
- 顶层传递 `expected_state_version`，它与整个 Host V2 Body 一起参与签名；
- Adapter 在领取 Ticket 前校验 Canonical Plan owner、当前 Step 与版本；
- 陈旧或终态操作返回 `conflict=true` 和最新 `plan`，不更新 Run、Turn 或 Trace；
- 若并发发生在前置检查之后，Core 事务的 Plan 行锁与版本条件只允许一个提交，Adapter 将失败方投影为同一冲突响应。

`contract.json` 是该增量契约的可执行清单；`scripts/validate_execution_ticket_contract.py` 同时校验 v1 与 v2。
