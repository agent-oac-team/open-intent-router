## 环境

- 日期：2026-07-20
- OAC Client：计划使用 `http://localhost:3000`
- OAC Go：`http://127.0.0.1:8182`
- OIR Host：`http://127.0.0.1:8280`
- OAC PostgreSQL：隔离本地实例 `127.0.0.1:55432`
- OIR Registry/Knowledge：隔离 SQLite fixture
- 三类固定测试 key ID 互不相同；报告不记录 secret/JWT/正文。

## 真实传输结果

| 场景 | 结果 |
| --- | --- |
| OAC `/api/central-agent-registry` -> OIR Registry | 200，返回 9 个 enabled Agent |
| 临时 Agent create/update/disable/enable/delete | 全部成功，revision 1..5 |
| Registry audit actor | 全部为真实 OAC admin user ID `2` |
| 临时 Agent 零代码路由 | user + 展业版命中临时 Agent，删除后不可见 |
| operator + 运营版 | 200，命中 `production_schedule` |
| user + 运营版 | 403 |
| user + 展业版 | 200，命中 `strategy_analysis` |
| JWT role 旧、数据库 role 已降级 | 403，数据库当前事实优先 |
| Knowledge Admin list | 200，6 个迁移资产 |
| Knowledge Admin multipart upload/delete | 200/200；真实 raw body HMAC 通过 |
| Coze asset discovery | 200，6 个资产，deferred `03` 可发现且 0 chunks |
| Coze `01` Exact Read | 200，44 chunks |
| Coze Knowledge write | 403，未到达 OIR 写 handler |
| V1 Host request | 401 `host_authentication_failed` |
| Capability | current/accepted 仅 V2，V1 disabled，三类 V2 终态计数可见 |
| OAC Client AI Sidebar | 使用 `localhost.:3000` 正常登录后加载成功，控制台无 Registry fallback warning |
| Registry/Knowledge 写超时 | 均只发起一次 downstream 请求，返回“提交状态未知”，无自动重试或 fallback |
| Header wire encoding | Route 中文 edition 仅在 JSON Body；Plan/Event Header 使用 ASCII Bundle ID，浏览器真实 Route 返回 200 且无 ISO-8859-1 异常 |

Registry 与 Knowledge 临时 fixture 均已删除；revision/audit 作为验收证据保留。未观察到
IRS fallback、双写或盲目写重试。

## 自动化门禁

- OAC Go：`go test ./...` 通过。
- OAC Client：TypeScript `--noEmit`、migration verifier、shared package build 通过。
- OIR：`915 passed, 1 skipped`；Ruff check 与 format check 通过。
- V2 contract：Python、Go、Node frozen vector validators 全部通过；基线索引共 71 个文件。
- Adapter/Core 边界扫描与 `openspec validate --strict` 通过。

## 未执行门禁

测试环境 100% transport replay 尚未执行。本地结果不能替代测试环境 V1 零成功、
current profile 正向/负向和 current-only capability 证据，因此本 change 暂不允许归档。
