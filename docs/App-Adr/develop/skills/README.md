# Develop Skills

本目录收录可重复执行的研发工作流和 Runbook。技能必须封装稳定的多步骤操作，并明确前置条件、输入、验证和回退；只有一条命令的说明不单独抽象为技能。

## 现有工作流

| 工作流 | 实体入口 | 用途 |
| --- | --- | --- |
| OpenSpec Explore / Propose / Apply / Archive | `.codex/skills/openspec-*` | 探索、提案、实现与归档 change |
| OpenSpec 状态判断 | [OpenSpec 交接](openspec-handoff.md) | 区分目录存在、任务完成、证据完成与可归档 |
| Memory Formation 上线 / 回退 | [Formation Runbook](runbooks/conversation-memory-formation-rollout.md) | off / observe / enforced 切换、质量门禁和紧急回退 |
| OAC Host Signature V2 | [V2 Runbook](runbooks/oac-host-signature-v2-runbook.md) | current-only V2 验收、提交未知和回滚 |
| OAC Host Runtime 迁移 | [Host 迁移 Runbook](runbooks/oac-host-migration.md) | Shadow、State Rehearsal、Fallback 和 Write Fence |
| IRS 切换 | [IRS 切换 Runbook](runbooks/irs-cutover.md) | 冻结、排空、切换、回滚和下线 |

## 候选领域技能

以下模式尚未稳定为独立技能，不为目录对称而提前制造：

- Core / Host 边界变更检查。
- OAC Legacy Contract 变更与 replay。
- Memory lifecycle / index repair 变更。
- 数据库 schema 与迁移证据生成。

当某模式至少重复出现三次、输入输出已稳定，且失败回退可验证时，再将其提炼到 `.codex/skills/` 并在本索引登记。
