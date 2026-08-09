# OAC Agent Entitlement 迁移与路由演练

日期：2026-07-20

## PostgreSQL Registry 演练

在隔离数据库 `oir_entitlement_rehearsal` 中导入 9 条旧中文 group Policy：

- dry-run：9/9 需要更新，非权限字段 hash 全部匹配基线。
- apply：9 条 definition 与 9 条 audit 同事务提交，revision 从 1 增至 2。
- repeat：`changes=0`，definition revision 与 audit 数量不增长。
- concurrent update：同一 expected revision 的两个请求恰好一个成功，另一个返回 `RegistryVersionConflict`。
- audited rollback：9 条 definition 恢复旧 Policy，新增 9 条 rollback audit，未直接改表或触碰 Turn/Run/Memory。

重点对账：

- `production_schedule -> [workspace.operations.access]`
- `strategy_analysis -> [workspace.operations.access, workspace.sales_enablement.access]`
- 9 个 Agent 的 stable ID、trigger、invocation、UI handoff、enabled 和非权限字段 hash 未变化。

## Transport E2E

使用隔离 SQLite OIR 数据库、临时 OAC PostgreSQL 和本地新版进程完成：

1. OAC Client 形状的登录与 `user_tags=[edition]` 请求。
2. OAC Go 查询当前 users role/status/approval，并签发 V2。
3. Adapter 验签、校验 Body/claims、展开 entitlement。
4. Core AccessPolicy 过滤 Registry 后执行 Router。

结果：

- `user + 展业版` 的 Router 日志只包含 5 个双版 Agent，不包含 `production_schedule`。
- `user + 运营版` 在 OAC Go 返回 `403`，未作为正常 `unsupported` 进入 Router。
- JWT 签发后把服务端 role 从 `user` 改为 `operator`，同一 subject 的运营版请求正向返回 `production_schedule/open_agent`，证明授权使用当前服务端事实；测试后 role 已恢复。
- `/capabilities` 报告 Central Route required=`v2`、claims=`oac-principal-v1`、policy=`oac-authz-v1`、Bundle catalog=`ok`。

本报告只覆盖 Agent 路由 entitlement，不覆盖 AuthorizationContext、在途撤权、知识权限或会话生命周期。
