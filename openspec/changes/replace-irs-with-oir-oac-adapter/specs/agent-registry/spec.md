## ADDED Requirements

### Requirement: 每个部署只有一个可写 Registry 事实源
系统 SHALL 显式配置 Registry 主写源，并在 OAC 迁移切换后将 OIR database Registry 设为唯一可写事实源；IRS、飞书和 file fallback MUST NOT 反向覆盖 OIR 定义。

#### Scenario: OIR database 为主写源
- **WHEN** OAC Host Runtime 以切换后配置启动
- **THEN** Agent CRUD 只写入 OIR database，文件仅在显式只读开发或故障模式下使用

#### Scenario: 试图启用飞书同步覆盖
- **WHEN** OIR 为主写时配置仍允许飞书定时或恢复同步写入
- **THEN** 系统 readiness 失败或拒绝同步，不允许已删除/禁用 Agent 被重新写回

### Requirement: Registry 变更必须版本化、可审计且防止静默覆盖
系统 SHALL 为每次 Agent Definition 创建、更新、启停和删除记录版本、操作者、时间、来源与前后差异，并对更新使用版本条件或乐观锁。

#### Scenario: 使用当前版本更新 Agent
- **WHEN** Admin 使用当前版本更新合法 Agent Definition
- **THEN** 系统保存新版本和审计差异，新定义在下一次 Registry 读取中可见

#### Scenario: 使用过期版本并发更新
- **WHEN** Admin 基于过期版本提交更新
- **THEN** 系统返回版本冲突，不静默覆盖已提交变更

### Requirement: Legacy Registry 映射不改变通用 Agent Definition 边界
系统 SHALL 通过 Host Adapter 映射 IRS/OAC Registry 专有字段，同时保持 OIR Agent Definition 可在无 OAC、Feishu、Coze、`bot_id` 和 `route_path` 概念时独立使用。

#### Scenario: 未来 Host 注册通用 Agent
- **WHEN** 非 OAC Host 创建不包含任何 OAC/Provider 专有字段的 Agent Definition
- **THEN** Registry 可完整持久化和路由该 Agent，不需要 OAC Adapter

#### Scenario: OAC Agent 包含 Provider/UI 信息
- **WHEN** Adapter 将旧 `bot_id/route_path` 转换为 provider config/metadata 和 `ui_handoff`
- **THEN** Registry 按通用 Schema 保存，公开视图和 LLM Prompt 仍脱敏

### Requirement: OAC 现有 Agent 迁移必须逐条对账
系统 SHALL 对 OAC 当前 9 个 Agent 逐条校验稳定 ID、名称、启用状态、权限、正/负触发、调用与 UI handoff 映射，MUST NOT 仅以导入数量作为成功证据。

#### Scenario: 9 个 Agent 数量相同但字段错误
- **WHEN** OIR Registry 中存在 9 条记录，但任一 Agent 的权限、启用状态、触发或 handoff 映射与基线不一致
- **THEN** 迁移验收失败并指出具体 Agent 与字段差异

#### Scenario: 所有 Agent 映射通过
- **WHEN** 每个稳定 `agent_id` 的契约、权限、调用与 UI 映射均通过对账
- **THEN** Registry 迁移门禁才能通过
