# Develop Standards

本目录定义 OIR 设计和编码的应用级标准。硬约束由根与作用域 `AGENTS.md` 自动加载；本文是可导航、可审查的完整清单。

| 编号 | 标准 | 可验证要求 |
| --- | --- | --- |
| D-001 | Core 通用性 | `app/` 不得出现 OAC、IRS、Coze、飞书、`bot_id`、`route_path` 等宿主专有契约；专有语义留在 `host_adapters/` |
| D-002 | 依赖方向 | `host_apps/oac -> host_adapters/oac -> app.application ports`；`app/` 不反向导入 Host，Adapter 不越过应用端口直接修改 Core repository 私有状态 |
| D-003 | 契约先行 | API / Schema 变更先明确 Pydantic 契约、错误语义和兼容策略，再修改 Service、Repository、Adapter 与测试 |
| D-004 | 事实源分层 | PostgreSQL canonical 数据高于 Host 展示消息、mem0 / Milvus 派生索引和 Debug 投影；派生索引必须可重建 |
| D-005 | 事务与幂等 | Turn、Run、Result、Plan、Outbox 等终态必须遵守现有事务边界；外部调用不持有数据库事务，重试不得产生第二个逻辑对象 |
| D-006 | 权限前置 | Agent、Memory、Knowledge、Host identity 的权限与所有权在模型、Provider 和响应正文之前校验；不能把 Prompt 当安全边界 |
| D-007 | 配置与密钥 | 配置定义以 `app/core/config.py` 或 `host_apps/oac/config.py` 为准，`.env.example` 只放占位值；日志、Trace、fixture 和文档不含真实凭证 |
| D-008 | OpenSpec 驱动 | 新能力、行为变化和破坏性迁移先建立 / 更新 OpenSpec；实现任务逐项勾选，归档前严格校验 |
| D-009 | 文档同步 | API、环境变量、数据表、运行流程或下游契约变化时，同步更新当前 API / 架构 / Runbook 和文档索引 |
| D-010 | 生成物边界 | `web/dist`、cache、egg-info 和脚本生成的迁移 JSON 不手工维护；修改源文件或重新执行权威生成脚本 |

## 作用域入口

| 修改范围 | 自动约束入口 | 进一步阅读 |
| --- | --- | --- |
| `app/**` | `app/AGENTS.md` | [App-Desc](../../../App-Desc/README.md) 与对应 Core 契约 |
| `docs/**` | `docs/AGENTS.md` | [文档中心](../../../README.md) 与 [App-Research](../../../App-Research/README.md) |
| `host_adapters/oac/**`、`host_apps/oac/**` | 各目录 `AGENTS.md` | OAC Host Adapter、兼容矩阵和 V2 Runbook |
| `tests/**` | `tests/AGENTS.md` | [Test Standards](../../../App-Adr/test/test-standards/README.md) |
| `web/**` | `web/AGENTS.md` | 可视化测试 UI 与中控运行图 |

根规则与作用域规则冲突时，以更严格且更接近目标文件的规则为准；任何规则都不能放宽安全、所有权和事实源边界。
