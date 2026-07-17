## ADDED Requirements

### Requirement: OAC Host Adapter 与 OIR Core 保持单向依赖
系统 SHALL 将 OAC–OIR Host Adapter 建设为独立 API、Schema、Mapper、Identity、Shadow、Fallback 和 Repository 模块，Adapter 只能调用 OIR 公开应用端口，OIR Core MUST NOT 反向依赖 Adapter 或 OAC/IRS/Coze 概念。

#### Scenario: Adapter 调用 Core 能力
- **WHEN** OAC 兼容请求通过 Adapter 进入同进程 Host Runtime
- **THEN** Adapter 通过 OIR 公开应用服务完成操作，不直接更新 Core Repository 私有状态

#### Scenario: Core 边界扫描
- **WHEN** CI 执行依赖方向和专有词检查
- **THEN** OIR Core API、Schema、Service、Prompt、配置与数据模型中不得出现 Adapter 反向 import 或 OAC/IRS/Coze 专有公共字段

### Requirement: Host Runtime 显式拥有 Legacy API Surface
系统 SHALL 由 OAC Host Composition Root 显式注册 IRS 兼容 API Surface，并将 OIR Native API 保持在独立前缀或监听入口；系统 MUST NOT 通过请求 Body 字段或 Handler 注册顺序猜测协议。

#### Scenario: Legacy 与 Native 共有 knowledge search 路径
- **WHEN** OAC Host profile 公开 `POST /api/v1/knowledge/search`
- **THEN** 该路径始终按 IRS 兼容 Schema 处理，OIR Native Search 使用独立入口

#### Scenario: 未启用 OAC Host profile
- **WHEN** 未来宿主以非 OAC profile 组装 OIR
- **THEN** OIR Native API 可独立启动且不需要 OAC Adapter 配置

### Requirement: Adapter 兼容 Central API 行为
系统 SHALL 兼容 `POST /api/v1/central/route`、navigation event、agent event 和 plan confirm 四类 IRS Central 方法，包括旧字段、8 种 route action、Plan 投影、`route_required`、HTTP 状态与用户可见错误语义。

#### Scenario: OAC 发起兼容 Route 请求
- **WHEN** 请求包含 IRS `user_query/current_agent/history/frontend_context` 字段
- **THEN** Adapter 将其转换为 OIR Native Route 命令，并将结果投影为 OAC 可解析的旧 Route/Plan 形状

#### Scenario: Agent Event 要求继续路由
- **WHEN** OIR 处理有效 Agent Event 后需要下一次路由
- **THEN** Adapter 返回与 IRS 契约兼容的 `route_required=true` 及必要引用

### Requirement: Adapter 兼容 Agent Registry API
系统 SHALL 兼容 IRS Agent Registry 的 GET、POST、PUT、enabled PATCH 和 DELETE 操作，并在 Adapter Mapper 中转换 provider/UI/标签专有字段。

#### Scenario: 旧 Agent 字段写入
- **WHEN** OAC Admin 提交包含 `bot_id/route_path/allowed_user_tags/positive_keywords/negative_keywords` 的 Agent
- **THEN** Adapter 将其分别映射到 provider config 或 Adapter metadata、`ui_handoff.route`、通用 access policy 与 trigger/examples，不新增 Core 专有一等字段

#### Scenario: 非法 OAC 页面路径
- **WHEN** Registry 写入中的 `route_path` 不是允许的 OAC 内部路径
- **THEN** Adapter 拒绝该请求，且不将值传入 OIR Registry

### Requirement: Adapter 完整兼容 IRS Knowledge API
系统 SHALL 实现 IRS 文档定义的 Search、Grouped Search、Exact Read、Asset/Chunk 查询与 Knowledge Admin 上传/列表/详情/删除/Chunks/Retry 全部契约，不得根据已知消费者调用情况裁剪接口。

#### Scenario: Search 兼容范围与返回选项
- **WHEN** 请求同时包含旧 `filters`、新 `scope` 和 `return_options`
- **THEN** Adapter 按 `scope` 优先、`filters` fallback 的规则调用 OIR，并只返回被请求的大字段与截断 warning

#### Scenario: Provider 失败使用结构化 warning
- **WHEN** Embedding、Milvus 或检索超时等 Provider 失败在 IRS 契约中应降级
- **THEN** Adapter 返回兼容的 HTTP 200、`matched=false`、空 evidence 和对应结构化 warning，不得将故障伪装成无 warning 的正常未命中

#### Scenario: Grouped Search 返回空资产槽位
- **WHEN** `content_production` 查询设置 `include_empty_assets=true` 且 `03` 客群资产无数据
- **THEN** 响应仍包含稳定 `assets.03`、`matched=false`、空 evidence、独立 warnings 和 trace

### Requirement: Identity Bridge 不信任可伪造业务字段
系统 SHALL 从受信 OAC 代理签名或服务身份建立 OIR UserContext，强制 `tenant_id=oac`，并为普通用户、Admin 和 Coze 使用分离的最小权限凭证。

#### Scenario: Body 伪造 tenant 或 user
- **WHEN** 兼容 Body 中的 tenant/user 与受信身份不一致
- **THEN** Adapter 使用受信身份并拒绝越权请求，不得把伪造值签发给 Core

#### Scenario: Coze 调用管理写接口
- **WHEN** `coze_workflow` 凭证尝试调用 Knowledge Admin 或 Registry 写入
- **THEN** Adapter 拒绝请求，且不复用 OAC Admin 身份

### Requirement: Adapter 提供脱敏 Capability 与健康契约
系统 SHALL 暴露 Adapter/Core/Schema/Policy 版本以及 Knowledge、Memory、Shadow、Fallback 模式和依赖健康，且 MUST NOT 暴露密钥、Ticket、Token 或完整敏感正文。

#### Scenario: 读取 Host Capability
- **WHEN** 授权运维调用方读取 capability/health
- **THEN** 响应足以判断当前兼容版本和运行模式，但所有敏感配置均被脱敏

### Requirement: OAC 对 execution ticket 的增强保持向后兼容
系统 SHALL 允许 OAC 在 Route 响应和 Agent Event 中透传可选不透明 `execution_ticket`，不得将其变为旧客户端的新必填字段。

#### Scenario: 新 OAC 客户端回传 Ticket
- **WHEN** OAC 收到含 `execution_ticket` 的外部 Agent Route 结果
- **THEN** OAC 将 Ticket 作为不透明值保存并在对应 Agent Event 中原样回传

#### Scenario: 旧客户端忽略 Ticket
- **WHEN** 旧 OAC 客户端忽略响应中的可选 Ticket
- **THEN** 原有 Route 响应仍可解析，而无 Ticket Event 只能按唯一受信服务端映射规则过渡
