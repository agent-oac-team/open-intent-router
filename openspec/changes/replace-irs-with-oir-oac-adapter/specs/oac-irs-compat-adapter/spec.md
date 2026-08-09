## ADDED Requirements

### Requirement: OAC Host Adapter 与 OIR Core 保持单向依赖
系统 SHALL 将历史 Central/Registry 兼容能力保持在独立 Host Adapter 中；Adapter 只能调用
OIR 公开应用端口，OIR Core MUST NOT 反向依赖 Adapter 或 OAC/IRS/Coze 概念。

#### Scenario: Adapter 调用 OIR 中控能力
- **WHEN** OAC Central 或 Registry 兼容请求通过 Adapter 进入 Host Runtime
- **THEN** Adapter 通过 OIR 公开应用服务完成操作，不直接更新 Core Repository 私有状态

#### Scenario: Core 边界扫描
- **WHEN** CI 执行依赖方向和专有词检查
- **THEN** OIR Core API、Schema、Service、Prompt、配置与数据模型中不得出现 Adapter 反向 import 或 IRS 专有运行依赖

### Requirement: OAC Central 兼容入口不得承载 Knowledge
系统 SHALL 只在 OAC Host Adapter 中兼容历史 Central Route、Navigation Event、Agent Event、
Plan Confirm 和迁移期 Registry 契约；Knowledge HTTP 路径 MUST NOT 注册到 OIR 或该
Adapter。

#### Scenario: OAC 发起 Central Route
- **WHEN** OAC 提交历史 Central Route 请求
- **THEN** Adapter 将请求转换为 OIR Native Route，并将结果投影为 OAC 可解析的兼容形状

#### Scenario: 调用方请求 Knowledge 路径
- **WHEN** 请求发送到 OIR 或 OAC–OIR Host Adapter 的 Search、Read、Assets、Chunks 或 Admin 路径
- **THEN** 该运行时不提供兼容 Handler，也不把请求代理到 `knowledge_sys` 或 IRS

### Requirement: OIR Core 可在无 OAC 与 IRS 配置时独立运行
系统 SHALL 允许通用 OIR 部署在未启用 OAC Host profile、未配置 IRS 地址且未配置
Knowledge Provider 的情况下完成路由、编排和 Memory 生命周期。

#### Scenario: 启动通用 OIR
- **WHEN** 部署未提供任何 OAC/IRS/Knowledge 专有配置
- **THEN** OIR Native API 正常启动，Knowledge optional 路径按未配置 Provider 的结构化状态处理

### Requirement: Identity Bridge 不信任可伪造业务字段
系统 SHALL 从受信 OAC 代理身份建立 OIR User Context，并拒绝把请求 Body 中的
tenant、user 或 tag 提升为未经验证的权限声明。

#### Scenario: Body 伪造 tenant 或 user
- **WHEN** 兼容 Body 中的 tenant/user 与受信身份不一致
- **THEN** Adapter 拒绝越权请求且不改变 OIR 中控事实

### Requirement: OAC 对 execution ticket 的增强保持向后兼容
系统 SHALL 允许 OAC 在 Route 响应和 Agent Event 中透传可选不透明 `execution_ticket`，
不得将其变为旧客户端的新必填字段。

#### Scenario: 新 OAC 客户端回传 Ticket
- **WHEN** OAC 收到含 `execution_ticket` 的外部 Agent Route 结果
- **THEN** OAC 将 Ticket 作为不透明值保存并在对应 Agent Event 中原样回传

#### Scenario: 旧客户端忽略 Ticket
- **WHEN** 旧 OAC 客户端忽略响应中的可选 Ticket
- **THEN** 原有 Route 响应仍可解析，而无 Ticket Event 只能按唯一受信服务端映射规则过渡
