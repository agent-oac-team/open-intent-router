# Test Standards

本目录定义 OIR 研发自测、测试设计、流水线门禁和环境证据的应用级标准。

| 编号 | 标准 | 可验证要求 |
| --- | --- | --- |
| T-001 | 风险分层 | 测试范围按变更影响选择，不用全量测试掩盖缺失的关键负向用例 |
| T-002 | 默认确定性 | 普通 CI 使用 memory/file/mock 等安全后端，不依赖真实 LLM、PostgreSQL、Milvus、外部 Host 或凭证 |
| T-003 | 契约快照保护 | `tests/contract/oac_irs` 是冻结兼容基线；只有经过版本化契约决策时才能更新，不能为让实现通过而改 fixture |
| T-004 | 权限负向矩阵 | identity、entitlement、tenant、user、subject、source policy 的正向用例必须配套伪造、越权、过期、重放或跨域负向用例 |
| T-005 | 事务失败注入 | 涉及 canonical 状态时覆盖中途失败、重复完成、并发冲突、重启恢复和提交未知 |
| T-006 | 真实环境证据分级 | 单元测试、本地 smoke、隔离 PostgreSQL 集成、测试环境 replay 和生产门禁是不同证据等级，报告不得互相替代 |
| T-007 | 数据隔离与清理 | 测试使用隔离 tenant / user / database / collection；脚本默认 dry-run 或生成可审计报告，不污染主数据域 |
| T-008 | 前端真实性 | 测试台只展示真实返回或明确的等待状态，不用定时器伪造后端内部进度；每轮 trace 不复用其他轮次或全局 Debug 数据 |
| T-009 | 文档可执行性 | Runbook 中的路径、命令、环境变量和脚本必须存在；带日期的通过数量只属于验收报告 |

## 变更到验证矩阵

| 变更面 | 最低验证 |
| --- | --- |
| Python 逻辑 / Schema | 受影响 pytest + `ruff check` + `ruff format --check` |
| Native API | Schema / Service / API 测试，并同步 `docs/App-Desc/contracts/api.md` |
| OAC Adapter / Identity | 对应 compat、identity、negative replay 与 boundary tests |
| Canonical Turn / Memory | transaction / outbox / lifecycle / recovery 聚焦测试；需要时运行隔离 PostgreSQL 测试 |
| Knowledge / Registry 迁移 | contract / golden / permission / idempotency；环境证据由对应脚本生成 |
| Web UI / TypeScript | `cd web && npm run test && npm run build` |
| OpenSpec | `openspec validate <change-name> --strict` |
| 仅文档导航 | `.venv/bin/python -m pytest tests/test_documentation_harness.py`、`git diff --check` |

流水线 job、本地命令、分支保护和定时巡检的完整要求见 [CI/CD 验收](ci-cd-acceptance.md)。
