## Context

IRS 目前同时承担 OAC 的 Central Route、Agent Registry、Knowledge Search/Read/Admin 和 Plan/Event 运行态。OAC Client 通过 Go 后端访问 Central API，OAC Go 与 Next.js 服务各自存在 Knowledge 调用链，Coze Workflow 还会直接调用 IRS 知识契约。外部聊天 Agent 由 OAC 执行，最终 Event 再回流 IRS。

OIR 已具有通用 Router、Registry、Invocation、Plan、Context、Memory 和基础 Knowledge Retrieval，但 OIR Native Schema 与 IRS/OAC 契约不同，而且 OIR 目前没有 IRS 的完整 Knowledge Asset/Read/Admin 能力，也没有宿主执行外部 Agent 时可回调收口的受信 Delegated Run。

当前约束：

- OAC 已停止运营，只处于开发状态，无生产环境和真实用户流量。
- OIR 必须最终完全替代 IRS，OAC 与 Coze 还需继续使用 IRS 对外契约。
- 首期固定 `tenant_id=oac`，OIR 使用独立 PostgreSQL database 和独立 Knowledge/Memory Milvus collections。
- Adapter 首期与 OIR 同仓、同进程，但 OIR Core 不得反向依赖 OAC/IRS/Coze 概念。
- OIR Context、Memory Recall 和自动 Formation 直接启用，但必须有隔离演练、mode-off 和可观测门禁。
- IRS 历史消息和存量 Session/Event/Plan/Result 不迁移也不保留；切流前必须排空。
- 原始知识输入为 `/Users/lijingtong/project/data/内容生产` 的 6 份工作簿，其中客群表当前为空。

主要利益相关方是 OIR 维护者、OAC Client/Go/Admin 开发者、Coze Workflow 调用方以及负责知识语义验收的业务维护者。

## Goals / Non-Goals

**Goals:**

- 以逻辑独立 Host Adapter 完整承接 IRS 的 OAC/Coze 对外契约。
- 使 OIR 成为 Agent、Knowledge、Canonical Turn、Run/Result、Plan/Event、Context 和 Memory 的唯一语义事实源。
- 用通用 Canonical Turn 和 Delegated Run 解决 OAC 外部执行 Agent 后的完整轮次收口问题。
- 从原始文件重建可追溯、可精确读取、可重建向量索引的 OIR Knowledge 数据域。
- 在无真实流量时，用可重复回放的 100% Shadow 证明契约、权限和核心语义满足切换门禁。
- 实现可证明未提交才回退的 Circuit/Fallback，以及阻止写入双主的 Write Fence。
- 排空 IRS 运行态后干净切换，最终停用 IRS 与飞书 Registry 同步。

**Non-Goals:**

- 不将 OAC 改造为 OIR Native Client，不要求 Coze Workflow 修改响应后处理。
- 不在 OIR Core 中引入 OAC 页面、`bot_id`、`route_path`、Coze 会话或飞书字段。
- 不迁移或保留 IRS/OAC 旧历史消息和存量运行态。
- 不在首期实现跨会话 Plan 续跑、历史结果复用或会话删除级联治理。
- 不复制 IRS Milvus 向量，不要求新旧 Chunk ID 或物理切块完全相同。
- 不定义生产 SLA、生产容量、生产灰度或发布日期。

## Decisions

### 1. Adapter 使用独立模块与 Composition Root

采用以下逻辑边界，具体 Python package 名可在实现时与现有仓库风格对齐：

```text
host_apps/oac
  -> host_adapters/oac/{api,schemas,mappers,identity,shadow,fallback,repositories}
  -> OIR bootstrap

host_adapters/oac
  -> OIR public application services / ports

OIR app/{api,schemas,services,repositories}
  -X-> host_adapters/oac
```

OAC Host Composition Root 负责组装 FastAPI Router、Adapter 配置与 OIR 依赖。Adapter 不得直接更新 Core Repository 私有状态，也不得自建 Turn/Run 状态机。CI 增加依赖方向和专有词扫描，保证同进程不演变为双向耦合。

不选择把兼容 Handler 直接放入 `app/api`，因为这会把 IRS/OAC Schema 变成 Core 的永久契约。首期也不拆成独立进程，避免在开发态迁移中过早引入网络故障面和额外部署单元。

### 2. Legacy 与 Native 入口由 Host 组装显式分离

OAC Host 的公开入口拥有 IRS 兼容路径，包括 Central 四类方法、Registry 五类方法和《OAC 共享知识库接口文档》列出的查询/读取/管理方法。OIR Native API 保持独立入口；如同一进程需要同时暴露 Native API，必须使用独立内部前缀或监听端口。

`POST /api/v1/knowledge/search` 存在同路径不同 Schema，因此禁止按 Body 字段猜测协议，也禁止依赖 FastAPI 注册顺序。Host 启动 profile 在 Composition Root 中决定公开路径所有者。

### 3. Identity Bridge 以受信身份覆盖兼容 Body

Adapter 在进入 Mapper 前验证 OAC 受信代理签名或服务身份，并强制注入 `tenant_id=oac`。Body 中的 `user_id/user_tags/consumer` 保留旧契约形状，但不作为最终授权证据。Adapter 通过允许列表将 OAC 标签映射为 OIR `groups/attributes`，再为 Core 生成短时受信身份签名。

OAC 普通用户、OAC Admin 和 Coze Workflow 使用分离的凭证与最小权限；Coze 不复用 Admin Token。签名算法与传输头在阶段 0 根据 OAC 现有网关能力固化，但必须具备 key rotation、timestamp/nonce、audience 和重放防护。

### 4. Canonical Turn 是语义轮次事实，不是展示消息副本

OIR 在接受语义 Route 请求时以 `(tenant_id, user_id, request_id)` 为主要幂等键创建 Canonical Turn。Turn 包含 `turn_id/tenant_id/user_id/session_id/request_id/source/status/state_version`、受控的用户输入快照、路由决策引用、Run/Result/Plan 引用、最终语义响应与时间戳。不将 OAC 消息 UI 字段或外部 Provider 会话 ID 放入一等字段。

Turn 分为两种收口路径：

- `reply/clarify/unsupported/silent` 等无 Agent 执行路径直接写入语义响应并完成 Turn，不伪造 Run。
- `open_agent/continue_agent` 等宿主执行路径将 Turn 保持为活动状态，预创建 Delegated Run，只有最终 Result 收口后才完成 Turn。

Turn 状态变更使用乐观版本或条件更新。同一幂等请求重试返回同一逻辑 Turn，终态 Turn 不得被迟到 Event 重开。

### 5. Delegated Run 由 Core 持有状态，Ticket 只是 Host 传输凭证

OIR 在外部副作用开始前创建 Run，并返回通用 execution reference。OAC Adapter 将其封装为不透明 `execution_ticket`，Ticket 至少绑定 `run_id/turn_id/tenant_id/user_id/agent_id/plan_id/step_id/purpose/expiry/nonce`，对外不暴露可篡改业务正文。

Adapter 只保存 Ticket hash 与生命周期状态。回调时先验证签名、所有权、用途、过期和 nonce，再通过 Core Application Service 提交通用 progress/completion command。Ticket 消费使用可恢复的 claim/lease：Core 提交成功后才标记 consumed，中途崩溃可以幂等重试。

旧 OAC 调用方未回传 Ticket 时，只有当 Adapter 能通过受信 request/event/plan/step 关联唯一命中一条未消费映射时才允许过渡；零命中或多命中必须拒绝，禁止仅根据 `session_id + agent_id` 猜测。

Run 完成通过一个 Core 事务同时收口 Run、Result、Plan Step、Turn 和 Transactional Outbox。Memory Formation 仅在主事务提交后消费 Outbox，不参与主交易且不能反向修改已完成 Turn。

### 6. Registry 以 OIR AgentDefinition 为唯一主写模型

IRS 字段通过 Adapter Mapper 转换：`bot_id` 进入 provider config 或 Adapter metadata，`route_path` 进入 `ui_handoff.route`，`allowed_user_tags` 进入通用 access policy，正/负关键词进入 trigger/examples。迁移后 OAC Registry Admin 兼容 API 直接写 OIR Registry，IRS 和飞书不再提供同步或恢复源。

Registry 写入增加版本、操作者、时间和前后差异，更新使用版本条件防止静默覆盖。现有 9 个 Agent 以逐条 ID/权限/触发/调用/UI 映射验收，不仅比较数量。

### 7. Knowledge 使用 PostgreSQL canonical store 和可重建 Milvus 索引

OIR 新增通用 Knowledge Asset/Chunk/Import Job/Asset Group/Migration Manifest 应用能力，其公共 Schema 不包含 OAC 业务名称。PostgreSQL 保存 canonical Asset/Chunk、状态、权限、稳定来源、内容哈希和 citation；Milvus `oir_knowledge_vectors` 只是可重建派生索引。Exact Read 只查 PostgreSQL，Milvus 不可用时仍可按授权读取 canonical 数据。

入库状态机覆盖 `uploaded/processing/indexed/failed/disabled/deleted`与 `parsing/chunking/embedding/indexing/cleanup` 阶段。首期为保持 IRS 同步上传契约，Adapter 等待应用服务返回兼容 Asset + Job 结果；内部仍持久化 Job，为以后转 Worker 保留边界。

首次导入按原始文件 SHA-256 幂等，每条 Chunk 保留 sheet/row/range 等 `source_ref`。`content_production` 组的 `01`~`06` 资产键和旧 asset ID 作为外部兼容契约保持稳定；空客群表记录 `deferred` 且不生成伪 Chunk。

Adapter Knowledge Facade 负责 IRS `filters/scope`、`return_options`、warning codes、grouped JSON 结构和 HTTP 语义的精确投影；通用 Core 只负责查询、权限、证据和生命周期不变量。

### 8. Shadow 以回放数据集为验收总体

因 OAC 无真实流量，100% 不表示采样比例，而表示回放总体覆盖率：每个固化 Contract 样本、Golden Query、权限负向用例和合成 E2E 时序都同时产生 IRS 主结果与 OIR Shadow 结果。

Decision Shadow 使用无持久副作用模式，禁止外部 Agent、页面动作和主命名空间的 Turn/Run/Plan/Memory 写入。State Rehearsal 在独立测试 database/schema 和 Milvus collection 中验证完整状态演进，保持逻辑 `tenant_id=oac` 而不用伪 tenant 绕过授权。

Diff 同时保存结构差异与行为指纹。Route 比较 action/agent/relation/plan/message 类型/延迟；Knowledge 比较 matched/evidence/排序/权限/warnings/延迟。文本不要求逐字等于 IRS，但权限放宽、跨用户数据、业务核心事实矛盾和重复写入属于阻塞级差异。

### 9. Fallback 由操作类型与提交证明共同决定

Adapter 为每个方法静态标注 `read_only/route_stateful/control_write/runtime_write`。只有 `read_only` 和能够证明 OIR 未提交的 `route_stateful` 可以自动回退 IRS。连接拒绝、DNS 失败、Circuit 已打开或 OIR 明确 `not_accepted` 可以作为未提交证据；超时且提交未知时禁止回退。

Registry、Knowledge Admin、Event、Plan Action 和 Memory 始终由 Write Fence 禁止自动回退和双写。所有 fallback、blocked fallback 和 ambiguous commit 都记录 operation class、reason、request ID、模式与版本，但不记录 Ticket/密钥或不必要正文。

### 10. 切换建立新 OIR 运行纪元

迁移只搬迁 Agent 定义并从原始文件重建 Knowledge，不搬迁 IRS/OAC 历史消息与运行态。切换前执行盘点脚本，停止 IRS 新写入，将所有 `pending/running/blocked` Plan、在途 Agent 和待回调 Event 完成或显式终止。

只保留 OpenAPI/JSON Schema/错误样本等契约快照、非敏感配置清单、排空报告和 cutover watermark，不保留历史消息正文和存量运行态数据。watermark 之前的迟到回调只进入隔离审计，不调用 OIR 状态变更端口。

OIR 从空 Turn/Run/Plan/Memory 运行态开始，旧 IRS ID 不作为可续跑 ID。这一选择牺牲旧会话连续性，但在已停运的开发环境中避免建设高风险且一次性的运行态转换器。

### 11. Capability 与可观测是运行时契约

OAC Host Runtime 暴露经脱敏的 capability/health 输出，至少包含 Adapter/Core/Schema/Policy 版本、Knowledge/Memory/Shadow/Fallback 模式和依赖健康。日志与 Trace 使用 `request_id/session_id/turn_id/run_id/result_id/event_id/plan_id/trace_id`关联，所有正文按长度与敏感策略受控。

配置分为 Core 与 Host profile 两层。Core 不读取 OAC/IRS 专有配置；Host profile 持有 legacy path、IRS fallback URL、identity key、Coze credential、shadow mode、operation policy 和 cutover watermark。敏感值只来自环境或 secret store，不进入文档、Diff 或 debug 响应。

## Risks / Trade-offs

- [Adapter 与 Core 同进程共享故障面] → 使用模块资源预算、超时、Circuit、依赖方向测试和可拆分 Composition Root。
- [Legacy/Native 同路径冲突] → 由 Host profile 显式拥有公开路径，不做 Schema 猜测或双 Handler 注册。
- [无真实流量导致 Shadow 空跑] → 将全量契约、Golden Dataset、负向权限与合成 E2E 回放作为固定验收总体并版本化。
- [Ticket 泄漏、伪造、重放或崩溃后误消费] → 仅传输不透明值、存 hash、绑定所有权/用途/有效期，采用 lease + Core 幂等收口。
- [OAC 最终 Agent 回答未回流 OIR] → 预创建 Delegated Run，对超时/孤儿 Run 告警，未完成 Turn 禁止 Formation。
- [Route 超时后回退产生双状态] → 提交未知时 fail closed，通过幂等状态查询证明未提交才允许 fallback。
- [重新解析造成知识语义漂移] → 保留原文/source_ref/hash/parser 版本，对核心事实、条件、金额、期限和权限执行 Golden 对账。
- [空客群表被误当作迁移失败] → 保留稳定 `03` 资产槽位与 `deferred` 状态，不创建空白 Chunk。
- [不保留历史数据降低排查能力] → 切换前先固化契约样本、非敏感配置、排空报告和 watermark，但不保留消息正文或运行态数据。
- [OIR 同时承接知识管理扩大核心范围] → 只把通用 Asset/Chunk/Policy/Read/Ingestion 放入 OIR 应用能力，IRS/OAC 字段、分组键和 warning 投影留在 Adapter。

## Migration Plan

1. **契约冻结**：抓取 IRS OpenAPI/Schema/请求/响应/错误/warning 样本，固化 Central、Registry、Knowledge 契约测试、Golden Queries 与 OAC 时序回放。
2. **Adapter 骨架与身份**：建立 Host Composition Root、Compat Schema/Mapper、Identity Bridge、capability endpoint 与依赖边界门禁。
3. **Canonical 运行态**：实现 Turn/Delegated Run/Result/Outbox 通用服务，并打通 Route-only 与外部 Agent Event 收口。
4. **Central 与 Registry 兼容**：实现 Central 四类方法、Registry 五类方法、Ticket 透传/过渡关联，迁移 9 个 Agent 并停用飞书同步。
5. **Knowledge 重建**：创建 OIR database/collections，实现通用 Asset/Chunk/Read/Admin，导入 6 份原始文件，生成 Manifest 并通过契约/语义/权限验收。
6. **本地 E2E**：在 OAC 本地联调中启用 Governed Context/Memory，验证完整 Turn、mode-off、dead-letter、修复与孤儿 Run 清理。
7. **回放式 100% Shadow**：IRS 返回对照结果，OIR 执行无副作 Decision Shadow，独立数据域执行 State Rehearsal，清零阻塞级 Diff。
8. **切换前排空**：冻结 IRS 新写入，完成或终止所有活动运行态，输出排空报告与 cutover watermark，不保留历史消息正文。
9. **测试环境切换**：OAC Go/Next.js 和 Coze Knowledge 入口指向 Adapter，OIR 从空运行态成为唯一主源，开启只读/安全 Route fallback 稳定窗口。
10. **下线 IRS**：验证无 IRS 活跃请求、无活动运行态与无未处置迟到回调，移除 fallback 并停用 IRS。

回滚仅允许在 IRS 尚未停用的稳定窗口内进行。回滚前冻结 OIR 新写入并记录 turn/request/run/event/plan/manifest watermark；只读 Knowledge 可切回 IRS，已完成的 OIR Memory、Plan、Run 不反向复制到 IRS。任何无法确定提交状态的写操作都不得用回退重放。

## Open Questions

以下项是阶段 0 必须用环境事实固化的实施参数，当前不改变已确认的产品范围；如验证结果需要改变架构或对外契约，必须暂停实施并重新人工确认：

- OAC 到 Adapter 最终使用现有网关签名、HMAC service header 还是 mTLS，以及 key rotation 位置。
- OAC Go 与 Next.js 的 `CENTRAL_API_BASE_URL` 及 Coze Knowledge Base URL 在本地/测试环境的最终切换入口。
- `execution_ticket` 在 OAC Route 响应、前端运行态和 Agent Event 请求中的精确可选字段位置及旧客户端忽略行为。
- IRS 实际 OpenAPI/错误/warning 样本与《OAC 共享知识库接口文档》冲突时的逐项兼容结论。
- 根据 IRS 本地基线固化的 Route/Knowledge 延迟、超时、Circuit 与稳定窗口阈值。
