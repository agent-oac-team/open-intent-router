# OAC 迁移说明

> 状态：历史概念映射。当前 OAC / IRS 迁移以 [OAC Host Adapter](../../App-Desc/architecture/oac-host-adapter.md)、[Registry 迁移与主写规则](../../App-Desc/architecture/registry-migration.md)、[OIR 替换 IRS 需求追踪矩阵](../tracking/requirements-traceability.md) 和对应 Runbook 为准。

`open-intent-router` 将 `/Users/lijingtong/project/intent_recon_sys` 作为参考实现和迁移来源，不把 OAC、银行私行业务、飞书或 Coze 作为核心依赖。

迁移目标是把原系统中的“意图识别、中控编排、子智能体注册与调用”沉淀为通用能力，同时把业务专属字段留在宿主应用或扩展配置中。

## 概念映射

| OAC 概念 | open-intent-router 概念 |
| --- | --- |
| 中控系统 / central | Intent Router / Agent Orchestrator |
| 理财经理用户 | End User / Operator |
| 子智能体 | Agent / Tool / Workflow |
| `bot_id` | `invocation.config` 或后续 Provider Adapter 配置 |
| `route_path` | `ui_handoff.route` |
| 飞书多维表格注册表 | 外部 RegistrySource 插件，不进入 MVP 核心 |
| OAC 权限标签 | `access_policy.roles/groups/tenants/attributes` |
| 知识库检索 | 可选 Evidence Provider 插件 |
| 合规质检、企微文案、海报生成 | 示例 Agent 或宿主应用专属 Agent |

## 推荐迁移步骤

1. 导出 OAC 现有 Agent 注册表数据。
2. 将每个子智能体转换为通用 Agent Definition。
3. 将 `bot_id`、平台 App ID、访问参数等放入 `invocation.config` 或 `provider_config`。
4. 将 `route_path` 转换为 `ui_handoff.route`。
5. 将 `allowed_user_tags` 等权限字段转换为 `access_policy`。
6. 将 OAC 专属 Prompt 约束迁出核心路由器，放入宿主应用策略或 Agent 配置。
7. 通过 `ui_handoff` 对接 OAC 前端页面跳转。
8. 在固定问题接口稳定后，把知识库检索迁移为 Evidence Provider 插件。

## 不建议迁移进核心的内容

以下内容应保留在宿主应用、插件或示例中：

- 飞书同步逻辑。
- 银行私行业务字段。
- 理财经理专属角色命名。
- Coze `bot_id` 等平台专属字段。
- OAC 前端路由表。
- 合规质检、企微文案、海报生成等具体业务 Agent 的硬编码逻辑。

## 迁移后的边界

核心项目负责：

- Agent Definition。
- Agent Registry。
- 用户上下文与访问控制。
- 意图识别与路由决策。
- 基础 Agent 调用。
- 事件、Run、Plan 和路由日志。

宿主应用负责：

- 具体业务页面。
- 业务权限体系与用户身份来源。
- 专属 Prompt 和业务术语。
- 前端路由跳转。
- 私有 Agent 平台凭证管理。
- 业务数据和知识库生命周期管理。

## 待确认事项

- 是否需要提供 OAC 注册表到 Agent Definition 的一次性转换脚本。
- 是否需要把 Coze 调用封装为独立 Provider Adapter 示例。
- 知识库检索插件是否复用旧系统索引结构，还是按新 Evidence Provider 接口重新实现。
