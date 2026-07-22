# Memory Runtime Mode 本地验收报告

日期：2026-07-21  
适用变更：`simplify-memory-runtime-mode`  
环境：本地 OIR PostgreSQL、隔离 SQLite、OAC `3000 -> 8182 -> 8280`

## 结论

`MEMORY_MODE=off|observe|on` 的代码、配置、Runtime 投影、Worker 生命周期和 Registry 配置已完成本地自动化验收。测试环境未执行；真实 OAC 登录后的 Formation -> Recall 浏览器闭环仍未完成，因此本 change 暂不具备归档条件。

## 已通过证据

| 验收面 | 结果 |
| --- | --- |
| OIR 全量 pytest | `963 passed, 1 skipped, 3 warnings`；唯一 skip 为未提供 `OIR_TEST_POSTGRESQL_URL` 的 Registry 并发锁测试；warnings 为 1 条 Starlette TestClient deprecation 和 2 条既有 aiosqlite event-loop-closed thread warning |
| Memory PostgreSQL integration | `4 passed` |
| Ruff | lint 与 format check 通过，300 个文件符合格式 |
| OIR Web | `47 passed`，Vite build 通过 |
| OAC Go | `go test ./...` 通过 |
| OAC Client | TypeScript check、`OIR_ADAPTER_MIGRATION_VERIFY_OK` 通过 |
| OpenSpec | `openspec validate simplify-memory-runtime-mode --strict` 通过 |

隔离 SQLite Runtime 依次实际启动三种模式：

| 模式 | Recall | Formation | Formation worker/sweeper | Index/TTL | Governed Context | Route scopes |
| --- | --- | --- | --- | --- | --- | --- |
| `off` | 关闭 | `off` | 关闭 | 开启 | 关闭 | 空 |
| `observe` | 关闭 | `observe` | 开启 | 开启 | 关闭 | 空 |
| `on` | 开启 | `enforced` | 开启 | 开启 | 开启 | `user_preference,stable_fact` |

三种模式均为 `memory-runtime-policy-v1`，Outbox 固定开启、Consolidation 固定关闭，隔离数据域的 queue/dead-letter 均为零。副作用、幂等、重启恢复、Decision Shadow、State Rehearsal、删除、TTL、index repair 和 scope 隔离由本轮全量测试覆盖。

## OAC 本地链路

本地 OIR Host 已按 `MEMORY_MODE=on` 重启在 `127.0.0.1:8280`，OAC Go 与 Client 分别运行在 `127.0.0.1:8182` 和 `localhost:3000`。Runtime/Capability 实际返回：

- `memory_mode=on`、`memory_policy_version=memory-runtime-policy-v1`、`memory_config_source=OAC_HOST_SHADOW_MODE`。
- Registry backend 为 PostgreSQL，共 9 个 Agent。
- `strategy_analysis`、`compliance_review` 为 `prefetch + user_preference/stable_fact + max_items=5`。
- 其余 7 个 Agent 均为 `memory.disabled`。

## 未完成与风险

1. OAC Client 全构建在无关共享包 `packages/api-contracts/src/admin-workforce.ts` 的 DTS 阶段失败：该包未声明 `URLSearchParams` 类型。此次 change 未扩张修复该基线问题。
2. Go 重启后浏览器中的旧 JWT 无法由新进程校验，真实登录态 Route 与 Formation -> Recall 浏览器验收未完成。没有读取浏览器存储或绕过登录。
3. 现有 OIR PostgreSQL 含历史 dead-letter：Formation 2 条 `formation_model_provider_error`、Index 1 条 `provider_connectionconfigexception`。本轮隔离三态启动没有新增 dead-letter，但真实 provider 闭环仍需在重新登录后复测并处理历史队列。
4. 未访问、部署或修改测试环境；`8.6/8.7` 保持未完成。

## 继续验收入口

1. 用户在 `http://localhost:3000` 重新登录后，执行真实 V2 HMAC Route。
2. 使用批准的两个 Agent 形成一条明确长期偏好，等待 Formation job 完成并确认 index ready。
3. 后续请求确认 Route/Agent recall 命中；同时确认其余 7 个 Agent 不获得 Agent-stage Memory。
4. 清理或审计历史 dead-letter，再重新读取 Runtime health。
5. 单独修复 OAC shared package 的 DTS lib 后重跑 `corepack pnpm build:client`。
