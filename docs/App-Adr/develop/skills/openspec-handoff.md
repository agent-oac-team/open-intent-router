# OpenSpec 交接与状态判断

OIR 使用 `openspec/` 保存行为变更的需求、设计、规格和任务。早期 `bootstrap-open-intent-router` 已归档；项目现状不能再由该 change 的 MVP 排除项推断。

## 状态判断

1. `openspec/specs/` 是已归档并生效的能力规格。
2. `openspec/changes/<change>/` 是尚未归档的变更工作区。
3. 一个 change 是否完成，以 `tasks.md`、严格校验和验收证据共同判断；目录存在不等于仍未实现，代码存在也不等于已满足环境门禁。
4. 当前 API、运行和迁移状态还应交叉核对 [文档中心](../../../README.md)、[应用地图](../../../App-Desc/README.md) 和 [需求追踪矩阵](../../../App-Research/tracking/requirements-traceability.md)。

## 当前交接重点

- `replace-irs-with-oir-oac-adapter` 仍有测试环境 replay、排空、cutover、IRS 下线和最终 P0 对账未完成。
- `fix-admin-hmac-and-retire-host-v1` 已在代码层收敛为 current-only V2，但测试环境 replay 和最终报告仍是归档门禁。
- 其他非 archive change 必须逐项读取自己的 `proposal.md`、`design.md`、`specs/**/spec.md` 和 `tasks.md`，不要使用本文件复制其动态任务状态。

## Agent 工作流

1. 探索问题时使用 OpenSpec Explore，不修改代码。
2. 新能力或行为变化使用 OpenSpec Propose 建立完整 artifacts。
3. 实现前读取 change 全部上下文，按 tasks 顺序工作并及时勾选。
4. 修改 change 后运行 `openspec validate <change-name> --strict`。
5. 只有任务、测试、文档和所需环境证据全部满足时，才使用 Archive 归档。

仓库内对应技能位于 `.codex/skills/openspec-*`，其目录见 [App-Adr](../../README.md)。
