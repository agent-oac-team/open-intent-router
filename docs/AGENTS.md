# OIR 文档作用域规则

本文件适用于 `docs/**`，补充根 `AGENTS.md`。

- 编辑前先读 `docs/README.md`，确认目标文档属于 App-Adr、App-Desc 还是 App-Research，并确认状态与当前权威来源。
- `docs/` 根目录只允许 `README.md` 和 `AGENTS.md`；新文档必须进入 `App-Adr/`、`App-Desc/` 或 `App-Research/` 并在对应索引登记。
- App-Adr 只存放强制约束、标准和可执行工作流；App-Desc 只存放当前地图、契约、架构和界面说明；App-Research 存放需求、设计取舍、过程记录、验收证据和生成报告。
- 移动或重命名前必须检查 README、OpenSpec、测试、脚本和跨项目引用；移动后必须同步所有仓库内引用。
- 当前说明以代码、测试和 OpenSpec 为事实源；历史文档使用状态提示和当前指针，不回写成不断膨胀的变更日志。
- Runbook 的命令、脚本、路径和环境变量必须存在；测试数量和环境结论必须带绝对日期并留在验收记录。
- `docs/App-Research/evidence/**/*.json` 是生成或采集证据，不手工编辑；通过对应脚本重新生成。
- 修改后运行 `tests/test_documentation_harness.py`，并用 `git diff --check` 检查格式。
