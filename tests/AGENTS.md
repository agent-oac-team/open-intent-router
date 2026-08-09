# OIR 测试作用域规则

本文件适用于 `tests/**`，补充根 `AGENTS.md`。

- 测试必须锁定行为和不变量，不只断言状态码或字段存在。
- `tests/contract/oac_irs/**` 是冻结 Legacy 基线。除非契约经过版本化批准，不得为迁就实现修改 fixture、hash 或 Golden 预期。
- 权限正向用例必须配套跨 tenant / user / subject、坏签名、越权、过期、重放或 denied source 等负向用例。
- canonical 状态变更需覆盖失败注入、重复请求、并发冲突、重启恢复和提交未知；禁止用 mock 成功路径代替事务验证。
- 默认测试不得依赖真实 LLM、PostgreSQL、Milvus、网络或凭证。真实环境测试必须显式隔离数据域、说明前置条件并清理测试数据。
- 本地 smoke、测试环境 replay 和 cutover evidence 是不同证据等级，测试名称和报告不得混淆。
- 测试数据不使用真实用户、客户、密钥或生产正文；Debug / report 断言同时检查脱敏。
- 运行范围按 [App-Adr 测试矩阵](../docs/App-Adr/README.md#变更到验证矩阵) 选择，并在无法运行某层验证时明确记录风险。
