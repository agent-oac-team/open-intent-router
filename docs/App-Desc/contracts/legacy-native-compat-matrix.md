# Legacy / Native API 兼容矩阵

| Legacy API | 操作类 | OIR 应用端口 | 自动回退 |
|---|---|---|---|
| `POST /api/v1/central/route` | route_stateful | Routing、Turn、Invocation（仅可信 v2 Route capability）、Delegated Run | 仅 `not_accepted` |
| `POST /api/v1/central/events/navigation` | runtime_write | Events | 禁止 |
| `POST /api/v1/central/events/agent` | runtime_write | Delegated Run、Events | 禁止 |
| `GET /api/v1/central/active-plan` | read_only | Plans | 禁止 |
| `POST /api/v1/central/plans/{id}/confirm` | runtime_write | Plans | 禁止 |
| `POST /api/v1/central/plans/{id}/steps/{step_id}/page-task-completion` | runtime_write | Plans（仅当前 `open_ui` Step） | 禁止 |
| `GET /api/v1/admin/agent-registry` | read_only | Registry | 按策略只读 |
| Registry POST/PUT/PATCH/DELETE | control_write | Registry | 禁止 |
| Knowledge Search/Grouped/Read | read_only | Knowledge Assets | 按策略只读 |
| Knowledge Assets/Chunks GET | read_only | Knowledge Assets | 按策略只读 |
| Knowledge Admin GET | read_only | Knowledge Assets | 按策略只读 |
| Knowledge Admin POST/DELETE | control_write | Knowledge Assets | 禁止 |

Native API 使用 `/oir/api/v1`。Legacy Schema 快照索引位于 `tests/contract/oac_irs/baseline-index.json`，冻结基线包含 22 个 OpenAPI operation、22 个成功 fixture、9 个错误 fixture、10 个 Route Golden、17 个 Knowledge Golden 和 6 个 OAC 时序。

Native Route/Invoke/Plan/Run/Session 使用 `oir-principal-v1` Envelope；Native Agent Event 使用
Header Execution Ticket。两者都不改变 OAC `OIR-HOST-V2` 请求签名或 Legacy JSON Schema，Adapter
继续在自身边界完成身份、body Ticket 和唯一关联投影。

页面任务完成入口仅供 OAC Go 以 V2 User Host 身份调用；它不属于冻结的 IRS Legacy fixture，也不能用
`/events/agent` 或无 Ticket 的旧关联规则替代。

Coze 只使用 Knowledge Legacy API。验收止于 HTTP 请求、响应、权限、warning 和 transport smoke，不覆盖 Workflow 收到响应后的处理。
