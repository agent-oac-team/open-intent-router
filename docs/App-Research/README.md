# App-Research：OIR 研究与经验索引

App-Research 保存需要背景、方案比较和取舍过程才能理解的材料，以及带日期和环境边界的验收证据。它不是规则集，也不是当前 API 说明；普通编码任务不应全量加载本目录。

## 使用方式

1. 先从 [文档中心](../README.md) 或 [App-Desc](../App-Desc/README.md) 确认当前事实。
2. 只有遇到相同复杂问题时，选择一到两份相关研究材料。
3. 研究结论已经落地时，按“当前权威”链接执行，不用历史 TODO 判断现状。
4. 新研究必须记录日期、状态、问题边界、已知事实、方案比较、决策和未决项。

## 需求研究

| 文档 | 研究状态 | 当前权威 / 使用说明 |
| --- | --- | --- |
| [开源意图识别项目需求分析](requirements/开源意图识别项目需求分析文档.md) | 历史启动基线 | 用于理解从 IRS 抽取通用 OIR 的早期判断；当前能力看 App-Desc 和代码 |
| [上下文模块改造需求](requirements/上下文模块改造需求分析文档.md) | 已实现需求基线 | 当前行为看 [Context Pipeline 验收](validation/governed-context-pipeline-acceptance.md) 和相关 tests |
| [OIR 替换 IRS 需求分析](requirements/OIR替换IRS中控系统需求分析文档.md) | 当前迁移需求基线 | 当前完成度看 [需求追踪矩阵](tracking/requirements-traceability.md) 和 change tasks |

## 设计与方案取舍

| 文档 | 研究状态 | 当前权威 / 使用说明 |
| --- | --- | --- |
| [中控能力设计](designs/中控能力设计文档.md) | 历史路线图，M1-M7 多数已实现 | 当前契约看 App-Desc 和各领域 OpenSpec |
| [对话记忆自动形成方案](designs/oir-conversation-memory-auto-write-strategy.md) | 已实现设计记录 | 当前上线操作看 [Formation Runbook](../App-Adr/develop/skills/runbooks/conversation-memory-formation-rollout.md) |
| [OAC 无感迁移方案](designs/oac-irs-to-oir-seamless-migration-plan.md) | 历史早期方案 | 身份和回滚已被 V2 Runbook、当前 Adapter 文档和追踪矩阵取代 |
| [OAC 迁移说明](designs/oac-migration.md) | 历史概念映射 | 当前 Registry / identity 语义看 App-Desc 中的 Registry 和 Host Adapter 文档 |
| [knowledge_sys 与 OIR 职责分离](designs/knowledge-sys-externalization-decision.md) | 2026-07-30 已接受决策 | `knowledge_sys` 独立拥有知识能力；OIR 仅依赖通用 Provider interface |

## 协作与交付记录

| 文档 | 研究状态 | 当前权威 / 使用说明 |
| --- | --- | --- |
| [中控系统对齐会议纪要](records/中控系统对齐会议纪要与后续工作清单.md) | 2026-07-01 历史会议记录 | 只用于追溯试点和协作背景；任务状态不再从会议待办判断 |
| [中控系统交付说明](records/中控系统交付说明.md) | 历史演示快照 | 当前能力和启动方式看根 README、App-Desc 和可视化测试 UI 文档 |

OpenSpec 的当前状态判断属于可执行工作流，见 [OpenSpec 交接](../App-Adr/develop/skills/openspec-handoff.md)。

## 测试与验收记录

| 文档 | 证据日期 / 状态 | 用途 |
| --- | --- | --- |
| [Agent Context 测试方案](validation/agent-context-memory-knowledge-test-plan.md) | 2026-07-08 | M5/M6 边界、风险和测试矩阵 |
| [Agent Context 测试报告](validation/agent-context-memory-knowledge-test-report.md) | 2026-07-09 | 缺陷复测、真实基础设施和遗留风险 |
| [调试台 E2E 测试方案](validation/memory-knowledge-debug-console-e2e-test-plan.md) | 2026-07-09 | Memory / Knowledge / UI 端到端造数与用例 |
| [调试台 E2E 测试报告](validation/memory-knowledge-debug-console-e2e-test-report.md) | 2026-07-09 | 本地 PostgreSQL / mem0 / Milvus / UI 证据 |
| [Context Pipeline 验收](validation/governed-context-pipeline-acceptance.md) | 变更验收记录 | legacy / observe / enforced 行为和回滚条件 |
| [OAC Agent Entitlement 演练](validation/oac-agent-entitlement-rehearsal.md) | 2026-07-20 | Registry 迁移、V2 transport 和路由权限证据 |
| [OAC-OIR 本地联调报告](validation/oac-oir-local-integration-report.md) | 2026-07-16 | Contract、Golden、故障和 Shadow 本地证据 |
| [OIR 中控承接与 IRS 退役门禁](validation/central-retirement-gate-local-evidence.md) | 2026-07-31，本地隔离测试通过 | Central/Registry/运行态能力映射、排空、水位线、快照与恢复演练 |
| [OIR Knowledge 副本清理本地证据](validation/oir-knowledge-replica-cleanup-local-evidence.md) | 2026-07-31，本地清理与加固验证通过 | 固定表/向量清理、无正文 Manifest、Memory 不变性与可恢复两阶段说明 |
| [IRS 知识补数验收](validation/irs-knowledge-transition-import-report.md) | 2026-07-21 | IRS 测试环境增量补数、数据对账和旧链路验证；不作为 OIR 后续知识研发任务 |
| [Memory Runtime Mode 本地验收](validation/simplify-memory-runtime-mode-local-acceptance.md) | 2026-07-21，部分通过 | 三态启动、Registry、自动化门禁与未完成浏览器/测试环境证据 |
| [Memory 显式配置与不变性本地基线](validation/oir-memory-configuration-invariance-local-baseline.md) | 2026-07-31，本地隔离通过 | 独立 `MEMORY_*` 配置、无正文 CRUD/Recall 基线与环境验证缺口 |

测试报告只证明当时的代码、环境和样本。当前测试是否通过必须重新执行对应命令；本地 smoke 也不能替代测试环境 replay、cutover 或生产门禁。

## 追踪与机器证据

- [OIR 替换 IRS 需求追踪矩阵](tracking/requirements-traceability.md)：已验证能力、尚缺环境证据和当前阻塞。
- [Agent Registry 对账报告](evidence/migration/agent-registry-reconciliation.json)
- [Canonical Knowledge 向量重建报告](evidence/migration/canonical-knowledge-vector-reindex.json)
- [Content Workbooks dry-run](evidence/migration/content-workbooks-dry-run.json)
- [Content Workbooks final](evidence/migration/content-workbooks-final.json)
- [Content Workbooks PostgreSQL final](evidence/migration/content-workbooks-postgresql-final.json)
- [Content Workbooks PostgreSQL 幂等验证](evidence/migration/content-workbooks-postgresql-idempotency.json)
- [Imported Knowledge PostgreSQL 验证](evidence/migration/imported-knowledge-postgresql-validation.json)
- [Imported Knowledge 验证](evidence/migration/imported-knowledge-validation.json)
- [IRS 本地 Runtime 排空证据](evidence/migration/irs-local-runtime-drain.json)
- [Central Retirement Gate 测试证据](evidence/migration/central-retirement-gate-test.json)
- [Central Retirement 非敏感快照](evidence/migration/central-retirement-snapshot-test.json)
- [OIR Memory 不变性本地证据](evidence/migration/oir-memory-invariance-local.json)
- [OAC Shadow Replay Runner smoke](evidence/migration/oac-shadow-replay-runner-smoke.json)
- [OIR Memory 无正文不变性本地基线](evidence/migration/oir-memory-invariance-local.json)
- [OIR Knowledge 清理 Dry Run](evidence/migration/oir-knowledge-cleanup-dry-run.json)
- [OIR Knowledge 清理执行报告](evidence/migration/oir-knowledge-cleanup-executed.json)
- [测试库 Knowledge 清理 Dry Run](evidence/migration/test-oir-knowledge-cleanup-dry-run.json)
- [测试库 Knowledge 清理执行报告](evidence/migration/test-oir-knowledge-cleanup-executed.json)

上述 JSON 由脚本生成或环境采集，不手工编辑。证据中的历史路径是生成当时的快照，不作为当前文档导航。

## 研究毕业规则

当研究结论满足以下任一条件时，应“毕业”到当前文档：

1. 已经成为长期架构不变量。
2. 已被实现并由测试锁定。
3. 已形成可执行 Runbook。
4. 相同问题被重复引用至少三次。

毕业动作包括：更新 App-Desc 中的当前 API / 架构或 App-Adr 中的 Runbook，在本索引标记“已实现”并添加指针；保留原文作为取舍记录，但不继续追加当前状态流水账。

## 不应进入 App-Research 的内容

- 一条命令即可说明的操作步骤：进入 App-Adr Runbook。
- Agent 每次编码都必须遵守的红线：进入 `AGENTS.md` / App-Adr。
- 当前模块、路径、契约和架构说明：进入 App-Desc。
- 单次无复用价值的临时排查记录：由 issue、PR 或 git history 承载。
