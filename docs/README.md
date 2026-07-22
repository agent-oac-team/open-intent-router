# OIR 文档中心

本文是 `open-intent-router` 文档的统一入口。最近一次按代码、测试、OpenSpec 和运行配置完成全量核对的日期为 2026-07-21。

`docs/` 只保留本入口和 [作用域规则](AGENTS.md) 两个根文件；其余文档和证据必须归入 App-Adr、App-Desc 或 App-Research。目录不只是导航标签，而是文档的责任边界。

稳定领域词义统一维护在根目录 [CONTEXT.md](../CONTEXT.md)。需求、设计、代码命名或历史材料与词汇表冲突时，应先纠正用词；实现状态仍按下文事实源优先级判断。

## Harness 三件套

| 物料 | 实体目录 | 回答的问题 | 加载方式 |
| --- | --- | --- | --- |
| App-Adr | [App-Adr/](App-Adr/README.md) | 允许怎么改、怎么测、怎么执行高风险流程 | `AGENTS.md` 自动加载硬约束，标准和技能按角色加载 |
| App-Desc | [App-Desc/](App-Desc/README.md) | 系统在哪里、当前怎么工作、哪份契约是权威说明 | 新任务、跨模块修改或不熟悉目录时读取 |
| App-Research | [App-Research/](App-Research/README.md) | 为什么这样决策、方案如何比较、某次验收证明了什么 | 只在相似复杂问题、架构取舍或历史验收核对时读取 |

## 先读什么

| 读者 / 任务 | 建议阅读顺序 |
| --- | --- |
| 第一次接触项目 | [项目 README](../README.md) -> [领域语言](../CONTEXT.md) -> [App-Desc 应用地图](App-Desc/README.md) -> [API 概览](App-Desc/contracts/api.md) |
| 修改 OIR Core | [Develop Standards](App-Adr/develop/develop-standards/README.md) -> [App-Desc](App-Desc/README.md) -> 相关 OpenSpec -> 对应领域文档 |
| 修改 OAC Host Adapter | [OAC Host Adapter](App-Desc/architecture/oac-host-adapter.md) -> [Legacy / Native 兼容矩阵](App-Desc/contracts/legacy-native-compat-matrix.md) -> 对应 OpenSpec |
| 修改 Context / Memory / Knowledge | [Develop Standards](App-Adr/develop/develop-standards/README.md) -> [Canonical Turn](App-Desc/architecture/canonical-turn-data-model.md) -> 对应 Runbook 和 OpenSpec |
| 设计或执行测试 | [Test Standards](App-Adr/test/test-standards/README.md) -> [Test Skills](App-Adr/test/skills/README.md) -> 相关测试方案 / Runbook |
| 排查历史决策 | [App-Research](App-Research/README.md) -> 相关需求、设计或验收记录 |

## 第一性原则

1. **约束必须进入执行路径。** 只有被 `AGENTS.md` 自动加载或被任务路由明确要求读取的规范，才算有效 Harness。
2. **地图与规则分离。** App-Desc 说明系统在哪里、依赖谁、事实源是什么；App-Adr 说明允许怎么改。
3. **研究不自动注入。** 大型需求、方案和报告只在相关问题出现时读取，避免历史结论污染普通编码任务。
4. **代码与测试优先。** 文档冲突时，先用当前代码、数据库约束、可执行测试和 OpenSpec 还原事实，再修正文档。
5. **引用优于复制。** 同一机制只保留一份当前权威说明，其余材料通过链接关联。

## 事实源优先级

文档冲突时按以下顺序判断：

1. 当前代码、数据库约束和可执行测试。
2. 当前变更目录中的 OpenSpec `spec.md`、`design.md` 和 `tasks.md`。
3. App-Desc 中的当前契约 / 架构与 App-Adr 中的当前 Runbook。
4. App-Research 中的需求、方案、会议纪要和验收报告。

历史文档解释“为什么这样设计”，不能覆盖已经落地的代码或当前 OpenSpec。变更是否完成以对应 `tasks.md` 的勾选和验收证据为准，不能只看目录名称或文档标题。

## 文档状态

| 状态 | 含义 |
| --- | --- |
| 当前 | 应与代码同步，可作为实现和运维依据 |
| 当前需求基线 | 仍约束未完成工作；实现状态另看 OpenSpec 和追踪矩阵 |
| Runbook | 可执行操作手册；命令和门禁必须保持可验证 |
| 验收记录 | 某一日期和环境的证据，不自动代表当前环境 |
| 已实现设计记录 | 保留设计取舍；当前行为仍以代码和当前契约为准 |
| 历史方案 / 快照 | 只用于追溯，不作为当前操作说明 |

## 维护规则

1. 新文档必须先回答“这是约束 / 技能、当前系统说明，还是研究 / 证据”，然后放入对应目录。
2. 不在 `docs/` 根目录新增其他文档，不恢复 `docs/harness/`。
3. 当前能力只维护一份权威说明；其他文档使用链接，不复制整段机制。
4. 行为或契约变更先更新 OpenSpec，再同步 App-Desc、App-Adr Runbook 和必要的 App-Research 验收记录。
5. 研究结论稳定并落地后，在 App-Research 索引中标为“已提升”，同时指向当前权威文档。
6. 文档使用绝对日期 `YYYY-MM-DD`；不使用“今天”“最近上线”等会失效的时间表达。
7. 迁移 / replay JSON 作为研究证据归入 `App-Research/evidence/`，只通过权威脚本重新生成。
8. 修改后运行 `.venv/bin/python -m pytest tests/test_documentation_harness.py` 和 `git diff --check`。
