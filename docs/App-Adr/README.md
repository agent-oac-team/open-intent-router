# App-Adr：OIR 应用规约与技能

App-Adr 是 OIR Harness 的执行约束层。根与作用域 `AGENTS.md` 负责自动加载的硬约束；本目录按研发和测试角色提供可按任务加载的详细标准、工作流与 Runbook。

## 目录结构

```text
App-Adr/
├── develop/
│   ├── develop-standards/   # 设计、编码、安全和文档标准
│   └── skills/              # OpenSpec、迁移和运维工作流
└── test/
    ├── test-standards/      # 测试分层、证据和 CI 门禁
    └── skills/              # 可复用测试技能的入口与提炼规则
```

## 角色入口

| 角色 / 任务 | 必读 | 按需读取 |
| --- | --- | --- |
| 研发 Agent | [Develop Standards](develop/develop-standards/README.md) | [Develop Skills](develop/skills/README.md) 与相关 Runbook |
| 测试 Agent | [Test Standards](test/test-standards/README.md) | [Test Skills](test/skills/README.md) 与 App-Research 中的历史方案 / 报告 |
| 文档治理 | `docs/AGENTS.md` | [文档中心](../README.md) 和各类目录索引 |

## 加载协议

1. 始终遵守根 `AGENTS.md`。
2. 修改文件前读取路径上最近的作用域 `AGENTS.md`。
3. 跨模块或不熟悉目录时，先读 [App-Desc](../App-Desc/README.md)。
4. 行为、契约或架构变更时，读取对应 OpenSpec change 的全部上下文。
5. 只有遇到设计取舍、迁移争议或历史缺陷时，才从 [App-Research](../App-Research/README.md) 选择相关材料。
6. 完成后按 Test Standards 的验证矩阵执行检查，并同步 App-Desc 中的当前文档。

## 执行层约束

App-Adr 目录本身不会被 coding agent 自动加载。真正的强制执行入口仍是根 `AGENTS.md` 与 `app/`、`docs/`、`host_adapters/oac/`、`host_apps/oac/`、`tests/`、`web/` 中的作用域规则。标准或技能只有被 `AGENTS.md` 引用或被任务路由要求读取时，才是有效 Harness 的一部分。
