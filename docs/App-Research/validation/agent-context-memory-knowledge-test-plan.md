# add-agent-context-memory-knowledge 最终测试方案

版本：v1.0
日期：2026-07-08
适用变更：`openspec/changes/add-agent-context-memory-knowledge`
适用项目：`open_intent_router`

## 1. 背景与测试目标

`add-agent-context-memory-knowledge` 将 M5 Memory 与 M6 Knowledge 从独立增强能力升级为平台级 Agent 上下文治理契约。测试目标不是只验证“可以召回内容”，而是验证：

- Agent Definition 能以通用 `context` 声明 memory 和 knowledge 需求。
- Router、Invoker、固定工作流节点只能按平台声明和策略执行检索。
- 下游 Agent 只接收确定性的 `memory_context` 和 `knowledge_context`，不感知 mem0、Milvus、Evidence Provider 等内部机制。
- memory 写入、召回、TTL、冲突、权限、可观测符合 OIR 治理要求。
- knowledge 搜索受 caller、purpose、user、tenant、subject、source policy 约束。
- 固定问仍是 Evidence Provider 路由规则，不被误做成固定 FAQ 答案。
- 在真实后端、超时、失败、禁用、空结果和权限拒绝下，系统可以安全降级。

本方案把“OpenSpec 已勾选完成”视为待验证假设。测试应主动寻找规格与实现之间的落差，尤其是治理边界、真实基础设施和端到端调用一致性。

## 2. 测试范围

### 2.1 覆盖模块

- Schema：`app/schemas/agent_context.py`、`app/schemas/agents.py`、`app/schemas/memory.py`、`app/schemas/knowledge.py`
- Service：`app/services/agent_context_service.py`、`app/services/memory_service.py`、`app/services/knowledge_service.py`
- Adapter：`app/services/memory_adapter.py`、`app/services/knowledge_vector_store.py`
- API：`app/api/memory.py`、`app/api/knowledge.py`
- 路由与调用：`app/services/router_service.py`、`app/services/invocation_service.py`
- Evidence：`app/plugins/evidence.py`
- 持久化：`app/db/models.py`、`app/repositories/database.py`、`app/db/session.py`
- 配置与示例：`app/core/config.py`、`config/agents.example.yaml`
- 文档与调试：`docs/App-Desc/contracts/api.md`、`docs/App-Desc/contracts/agent-definition.md`、`docs/App-Desc/architecture/evidence-provider.md`、本地可视化测试 UI

### 2.2 不覆盖范围

- 不验证 RAG-Anything 集成。
- 不验证固定 FAQ 答案能力，因为本变更明确不提供该能力。
- 不验证普通聊天 UI 的用户记忆确认提示，因为本变更只要求 debug/admin 可见。
- 不验证真实外部 LLM 推理质量，只验证路由、上下文组装和调用契约。

## 3. 当前最大风险与测试重点

### 3.1 真实基础设施可能是假集成

OpenSpec 设计中标准开发基础设施是 PostgreSQL、Milvus、mem0。但当前实现可能大量使用内存仓库和 fallback。必须验证：

- `storage_backend=database` 下 Agent context、run input、route log、memory、knowledge 是否真实持久化。
- `memory_strategy_provider=mem0` 时，mem0 缺失、异常、返回格式差异是否被正确处理。
- `knowledge_vector_backend=milvus` 时，Milvus 依赖缺失或连接失败是否结构化降级，而不是误报成功。

### 3.2 声明式 context 可能不是强访问边界

`/knowledge/search` 是通用 API，caller metadata 由请求传入。必须验证：

- Agent 声明的 source 只是请求来源，不是最终授权证明。
- Agent 未声明的 source 不能通过 controlled retrieval 或直接 API 绕过。
- caller_type、caller_id、purpose 不能被滥用成越权依据。

### 3.3 端到端链路可能重复预取或语义不一致

Route preview 会注入 context，实际 invoke 也可能触发 context assembly。必须验证：

- route preview、invoke_from_route、direct invoke 三条链路行为一致。
- 已带 `memory_context` 或 `knowledge_context` 时不重复预取，不覆盖已有上下文。
- 部分带 context 的输入不会造成另一种 context 缺失或状态错误。

### 3.4 合法边界值可能被实现吞掉

重点检查 `max_items=0`。Schema 允许 0，但预取链路如使用 `config.max_items or default`，会把 0 回落为默认值，造成“配置关闭检索却仍然检索”的风险。

## 4. 测试环境

### 4.1 基础环境

- Python 虚拟环境：项目 `.venv`
- 后端测试命令：`.venv/bin/python -m pytest`
- 静态检查命令：
  - `.venv/bin/python -m ruff check .`
  - `.venv/bin/python -m ruff format --check .`
- OpenSpec 校验命令：`openspec validate add-agent-context-memory-knowledge --strict`
- 前端测试命令：
  - `cd web && npm run test`
  - `cd web && npm run build`

### 4.2 环境分层

| 环境 | 目的 | 配置重点 | 是否阻塞验收 |
| --- | --- | --- | --- |
| 内存环境 | 快速单元和契约测试 | `storage_backend=memory`、memory vector fallback | 阻塞 |
| SQLite 数据库环境 | 本地持久化和迁移测试 | `storage_backend=database`、临时 sqlite 文件 | 阻塞 |
| PostgreSQL 环境 | 标准元数据后端冒烟 | PostgreSQL metadata/log 表 | 条件阻塞 |
| mem0 环境 | memory 策略层真实集成 | `memory_strategy_provider=mem0` | 条件阻塞 |
| Milvus 环境 | knowledge 向量后端真实集成 | `knowledge_vector_backend=milvus` | 条件阻塞 |

条件阻塞含义：如果当前迭代声明已完成真实基础设施支持，则必须通过；如果只交付 adapter 边界和 fallback，则必须明确记录未完成范围，不得以内存测试通过替代真实集成通过。

## 5. 测试数据设计

### 5.1 用户与租户

| ID | 角色 | 租户 | 用途 |
| --- | --- | --- | --- |
| `u_operator_t1` | `operator` | `t1` | 普通允许用户 |
| `u_admin_t1` | `admin` | `t1` | 管理员用户 |
| `u_operator_t2` | `operator` | `t2` | 跨租户隔离 |
| `u_no_role_t1` | 无匹配角色 | `t1` | 权限拒绝 |

### 5.2 Agent

| ID | context 配置 | 用途 |
| --- | --- | --- |
| `agent_no_context` | 默认 disabled | 验证不预取 |
| `agent_memory_only` | memory prefetch，knowledge disabled | 验证仅 memory 注入 |
| `agent_knowledge_prefetch` | knowledge prefetch | 验证 knowledge 显式开启 |
| `agent_both_prefetch` | memory + knowledge prefetch | 验证完整上下文 |
| `agent_controlled_knowledge` | knowledge controlled_retrieval | 验证固定模板检索 |
| `agent_restricted` | access_policy 限制 | 验证固定问无权限 |

### 5.3 Memory

| ID | scope | subject/user/tenant | 状态 | 用途 |
| --- | --- | --- | --- | --- |
| `mem_pref_lang` | `user_preference` | `u_operator_t1/t1` | long-lived | 用户偏好召回 |
| `mem_fact` | `stable_fact` | `u_operator_t1/t1` | long-lived | 稳定事实召回 |
| `mem_task_active` | `task_memory` | `u_operator_t1/t1` | 未过期 | TTL 内召回 |
| `mem_task_expired` | `task_memory` | `u_operator_t1/t1` | 已过期 | cleanup 和过滤 |
| `mem_other_tenant` | `user_preference` | `u_operator_t2/t2` | active | 跨租户隔离 |
| `mem_agent_specific` | `stable_fact` | 指定 agent_id | active | agent 可见性 |

### 5.4 Knowledge Source

| ID | 权限 | 状态 | 用途 |
| --- | --- | --- | --- |
| `docs_public` | allow_tenants=`t1` | enabled | 正常命中 |
| `docs_admin` | allow_roles=`admin` | enabled | 普通用户拒绝 |
| `docs_disabled` | allow_tenants=`t1` | disabled | 禁用源拒绝 |
| `docs_t2` | allow_tenants=`t2` | enabled | 跨租户拒绝 |
| `docs_tagged` | tags=`product` | enabled | source_tags 过滤 |
| `docs_missing` | 不注册 | missing | 缺失源记录 denied |

## 6. 测试策略

### 6.1 Schema 与配置契约测试

目标：确保 Agent Definition context 是稳定、通用、严格的配置入口。

重点：

- 默认 context 为 memory disabled、knowledge disabled。
- `mode` 只允许 `disabled`、`prefetch`、`controlled_retrieval`。
- memory scope 只允许 `user_preference`、`stable_fact`、`task_memory`、`artifact_reference`、`session_summary`。
- `max_items` 边界为 0 到 50，51 应拒绝。
- controlled retrieval 必须配置 `controlled_retrieval`。
- context 不允许混入 host-specific 必填字段。
- Agent YAML 示例可被加载并保留 context。

### 6.2 Memory governance 测试

目标：验证 OIR 对 memory 生命周期和可见性的治理，而不是只验证 repository 搜索。

重点：

- `memory_enabled=false` 时 recall/write 返回 disabled 或 rejected。
- scope 过滤、subject 过滤、user 过滤、tenant 过滤、agent_id 过滤生效。
- 过期 memory 不进入 context，cleanup 删除并写入 `memory_expired` event。
- task memory、session summary、artifact reference 默认 14 天 TTL。
- user preference、stable fact 默认长期。
- 低置信度、空内容、敏感标记写入被拒绝。
- 当前输入与长期记忆冲突时，本轮输入优先，但不直接改写长期记忆，并记录 conflicts。
- debug/admin 能看到写入、拒绝、召回和事件信息。

### 6.3 Knowledge retrieval 测试

目标：验证 `/knowledge/search` 是通用受治理检索 API。

重点：

- caller_type 覆盖 `router`、`agent`、`host`、`admin`。
- purpose 覆盖 `route_evidence`、`agent_execution`、`debug`、`preview`。
- source policy 按 role、group、tenant、enabled、tag 生效。
- requested source 缺失、禁用、无权限时进入 denied metadata/log。
- 所有 requested sources 被拒绝时返回 empty/degraded，不阻塞调用。
- 命中时返回 items、summary、citations、source_ids。
- top_k 边界 0、1、50、51。
- provider timeout 和 provider error 返回结构化 status 和 errors。
- retrieval log 记录 query、caller、purpose、selected_source_ids、denied_source_ids、hit_count、status、errors。

### 6.4 AgentContextAssembly 测试

目标：验证 context 组装是 Router/Invoker 共享的确定性逻辑。

重点：

- memory prefetch 仅在 `context.memory.mode=prefetch` 时执行。
- knowledge prefetch 仅在 `context.knowledge.mode=prefetch` 时执行。
- disabled 模式稳定返回 disabled context。
- memory 与 knowledge 并发执行，单侧 timeout 不影响另一侧。
- context item 超过预算时截断并标记 `truncated=true`。
- `max_items=0` 必须返回空 context，不得回落默认值。
- controlled retrieval 模板只渲染 allowed_variables，未授权变量置空。
- controlled retrieval 结果结构与 `knowledge_context` 一致。

### 6.5 Router 与 Invoker 端到端测试

目标：验证最终进入 Agent 的输入稳定、可追踪、无重复检索。

重点：

- Route 到目标 Agent 后，InvocationPreview.input 包含 context 字段。
- Direct invoke 未带 context 时自动组装。
- invoke_from_route 已带 context 时不重复组装。
- input 只带 `memory_context` 或只带 `knowledge_context` 时，行为需符合设计预期并被测试锁定。
- run input、result、route metadata 记录 context status 和 item count。
- missing required inputs 时不应提前预取或泄露 context。
- invocation output schema 校验失败时，run 仍保留输入 context 供排查。

### 6.6 Evidence Provider 测试

目标：验证固定问仍是路由阶段证据，不混入 Agent 执行知识。

重点：

- 多 provider 按顺序调度，记录 selected、skip、error、timeout。
- strong fixed question 命中可用 Agent 时跳过 LLM。
- strong fixed question 命中不可用 Agent 时返回无权限，不 fallback LLM。
- weak fixed question 只进入 weak context，不收窄 LLM 候选集。
- fixed question 不返回直接 FAQ 答案，不绕过 KnowledgeSource policy。
- strong override 不被普通 evidence budget trimming 丢弃。

### 6.7 API 合约测试

目标：验证外部调用协议稳定。

重点接口：

- `POST /api/v1/memories/recall`
- `POST /api/v1/memories/write-candidates`
- `POST /api/v1/memories/cleanup`
- `GET /api/v1/memories/debug`
- `POST /api/v1/knowledge/search`
- `GET /api/v1/knowledge/debug`

重点：

- 正常响应结构与 response_model 一致。
- 非法 enum、非法 max_items、缺 user、缺 query 返回 422。
- debug limit、scope/source 参数解析正确。
- write-candidates 的 `user_id`、`tenant_id` query 参数缺失或异常有明确行为。
- API 不返回真实凭证或未脱敏敏感 metadata。

### 6.8 持久化与迁移测试

目标：验证数据库后端不是只支持旧字段。

重点：

- 旧 `agent_definitions` 表升级后新增 `context_text`。
- Agent upsert 后 context 能持久化并重新加载。
- AgentRun.input 中 context 字段能持久化并读取。
- RouteLog parsed_output 中 agent_context metadata 能持久化并读取。
- memory_items、memory_events、knowledge_sources、knowledge_chunks、knowledge_retrieval_logs 表结构完整。
- JSON 字段序列化和反序列化后不丢失 `citations`、`errors`、`metadata`。

### 6.9 真实基础设施集成测试

目标：避免内存 fallback 掩盖真实集成缺口。

重点：

- PostgreSQL：create_all_tables、agent context round trip、memory/knowledge metadata round trip。
- mem0：search/add/extract 的最小可用流程、异常 fallback、返回字段映射。
- Milvus：backend 配置、依赖缺失错误、连接失败错误、fallback 是否符合设计声明。
- 如果真实 Milvus 搜索尚未实现，测试结果必须标记为功能缺口，而不是通过内存搜索冒充。

### 6.10 安全、权限与隐私测试

目标：证明平台治理边界有效。

重点：

- 跨租户 memory 不可召回。
- 跨 subject memory 不可召回。
- source policy 拒绝的 knowledge 不出现在 items、summary、citations。
- caller_type 伪造成 admin 不应自动获得 source 权限。
- Agent context 声明外 source 不应通过受控链路返回。
- debug/admin 输出应限制敏感字段，不暴露 API key、token、真实客户信息。
- automatic memory write 不在普通 chat UI 弹 prompt，但 debug 可见。

### 6.11 性能与稳定性测试

目标：验证新增预取不显著拖慢主路径。

建议指标：

- memory prefetch timeout：500 到 800 ms 内可配置，超时结构化降级。
- knowledge prefetch timeout：800 到 1500 ms 内可配置，超时结构化降级。
- Router route 在 memory/knowledge 双 timeout 下仍返回可用结果。
- 并发 20 到 50 个 route/invoke 请求时，context 不串用户、不串 session。
- 超长 memory/knowledge item 被截断后，summary 和 metadata 不异常膨胀。

## 7. 核心测试用例矩阵

| ID | 优先级 | 类型 | 场景 | 预期 |
| --- | --- | --- | --- | --- |
| AC-001 | P0 | Schema | 默认 Agent context | memory/knowledge 均 disabled |
| AC-002 | P0 | Schema | 非法 mode | 422 或 ValidationError |
| AC-003 | P0 | Schema | 非法 memory scope | ValidationError |
| AC-004 | P0 | Schema | controlled_retrieval 缺模板 | ValidationError |
| AC-005 | P0 | Schema | `max_items=0` | 合法且执行时不检索 |
| AC-006 | P0 | Schema | `max_items=51` | ValidationError |
| MEM-001 | P0 | Service | memory disabled recall | status=disabled |
| MEM-002 | P0 | Service | memory disabled write | status=rejected，reason=memory_disabled |
| MEM-003 | P0 | Service | scope 过滤 | 只返回声明 scope |
| MEM-004 | P0 | Service | tenant 隔离 | 不返回其他 tenant memory |
| MEM-005 | P0 | Service | subject 隔离 | 不返回其他 subject memory |
| MEM-006 | P0 | Service | agent_id 隔离 | 不返回其他 agent 专属 memory |
| MEM-007 | P1 | Service | task memory TTL | 默认约 14 天过期 |
| MEM-008 | P1 | Service | expired cleanup | 删除过期项并记录事件 |
| MEM-009 | P0 | Service | 敏感候选写入 | rejected，debug 可查 |
| MEM-010 | P1 | Service | 当前输入冲突 | metadata.conflicts 记录 override |
| KNO-001 | P0 | API/Service | knowledge disabled | status=disabled |
| KNO-002 | P0 | API/Service | public source 命中 | items、summary、citation 完整 |
| KNO-003 | P0 | API/Service | role 拒绝 | denied_source_ids 记录，items 不含该源 |
| KNO-004 | P0 | API/Service | tenant 拒绝 | denied_source_ids 记录 |
| KNO-005 | P0 | API/Service | disabled source | 被拒绝或不可用，不返回 items |
| KNO-006 | P1 | API/Service | missing source | denied_source_ids 记录 missing |
| KNO-007 | P0 | API/Service | 所有 source 被拒绝 | status=empty 或 degraded，调用继续 |
| KNO-008 | P0 | API/Service | top_k=0 | 不返回 items，不回落默认 |
| KNO-009 | P1 | API/Service | provider timeout | status=timeout，log 记录 |
| KNO-010 | P1 | API/Service | provider error | status=error，log 记录 |
| CTX-001 | P0 | Assembly | no_context agent | 两个 context 均 disabled |
| CTX-002 | P0 | Assembly | memory only | 只执行 memory prefetch |
| CTX-003 | P0 | Assembly | knowledge disabled default | 不执行 knowledge prefetch |
| CTX-004 | P0 | Assembly | both prefetch | 两个 context 注入 invocation_input |
| CTX-005 | P0 | Assembly | memory timeout | memory status=timeout，knowledge 可正常 |
| CTX-006 | P0 | Assembly | knowledge timeout | knowledge status=timeout，memory 可正常 |
| CTX-007 | P0 | Assembly | `max_items=0` | context items 为空 |
| CTX-008 | P1 | Assembly | per item 截断 | content 截断，truncated=true |
| CTR-001 | P0 | Controlled | allowed variable | 正常渲染 query |
| CTR-002 | P0 | Controlled | 未授权变量 | 被置空，不进入 summary/log |
| CTR-003 | P0 | Controlled | 请求未授权 source | source denied，不返回 item |
| RTR-001 | P0 | E2E | route preview 注入 context | invocation.input 含稳定字段 |
| RTR-002 | P0 | E2E | fixed strong override | 跳过 LLM，仍 attach context |
| RTR-003 | P0 | E2E | fixed strong denied | 不调用 LLM，返回无权限 |
| RTR-004 | P1 | E2E | weak hint | 不收窄候选集 |
| INV-001 | P0 | E2E | direct invoke 自动组装 | run.input 含 context |
| INV-002 | P0 | E2E | invoke_from_route 不重复预取 | 召回次数不增加，context 不变 |
| INV-003 | P0 | E2E | 输入已带部分 context | 行为符合设计并被测试锁定 |
| INV-004 | P1 | E2E | Agent 输出 schema 失败 | run.input 保留 context |
| DB-001 | P0 | DB | legacy agent_definitions 迁移 | 新增 context_text |
| DB-002 | P0 | DB | Agent context round trip | upsert/list/get 不丢字段 |
| DB-003 | P0 | DB | AgentRun input round trip | context JSON 不丢字段 |
| DB-004 | P1 | DB | Knowledge log round trip | denied/errors/hit_count 不丢 |
| SEC-001 | P0 | Security | caller_type 伪 admin | 不自动越权 |
| SEC-002 | P0 | Security | Agent 声明外 source | 不通过受控链路返回 |
| SEC-003 | P0 | Security | debug 输出脱敏 | 不暴露 token/key |
| PERF-001 | P1 | Perf | 双预取并发 | 总耗时接近较慢一侧 timeout |
| PERF-002 | P2 | Perf | 并发 route/invoke | 不串 user/session/context |
| DOC-001 | P1 | Docs | docs/api 与 schema 对齐 | 字段、状态、边界一致 |
| DOC-002 | P1 | Docs | agent-definition 示例可加载 | context 示例有效 |

## 8. 端到端主路径

### 8.1 Memory only Agent

步骤：

1. 注册 `agent_memory_only`，配置 `context.memory.mode=prefetch`，knowledge 默认 disabled。
2. 写入 `mem_pref_lang`。
3. 用户 `u_operator_t1` 发起 route。
4. Router 选择 `agent_memory_only`。
5. 检查 InvocationPreview.input。

预期：

- `memory_context.status=ok`
- `memory_context.items` 包含 `mem_pref_lang`
- `knowledge_context.status=disabled`
- route metadata 包含 agent_context status 和 item_count

### 8.2 Memory + Knowledge Agent

步骤：

1. 注册 `agent_both_prefetch`。
2. 写入用户 memory 和 `docs_public` chunk。
3. route 到该 Agent。
4. 执行 direct invoke。
5. 查询 run log 和 knowledge debug log。

预期：

- preview input 和 run input 都包含 context。
- knowledge citations 包含 source_id、chunk_id、title、uri。
- retrieval log 记录 caller_type、caller_id、purpose、selected_source_ids。

### 8.3 Controlled Retrieval

步骤：

1. 注册 `agent_controlled_knowledge`。
2. 配置模板：`{{ input.text }} {{ secret }}`。
3. allowed_variables 只允许 `input.text`。
4. variables 传入 `secret=internal-token`。

预期：

- query 和 summary 不包含 `internal-token`。
- 只返回允许 source。
- denied source 进入 metadata。

### 8.4 Fixed Question Strong Override

步骤：

1. 配置 strong fixed question 指向 `agent_both_prefetch`。
2. 使用会命中固定问的输入。
3. LLM 使用会失败的 fake client。

预期：

- LLM 调用次数为 0。
- response.decision 指向 fixed target。
- 如果 target 可用，则仍 attach invocation context。
- 如果 target 不可用，则返回无权限，不 fallback LLM。

### 8.5 Timeout Degradation

步骤：

1. memory_service 和 knowledge vector store 分别模拟超时。
2. route 到 `agent_both_prefetch`。

预期：

- route/invoke 不失败。
- `memory_context.status=timeout` 或 `knowledge_context.status=timeout`。
- errors 字段包含 timeout code。
- route metadata 和 log 可追踪超时。

## 9. 自动化测试组织建议

建议在现有测试基础上补充：

- `tests/test_agent_context_memory_knowledge.py`
  - 补充 `max_items=0`
  - 补充 disabled 开关
  - 补充 partial existing context
  - 补充 source_tags、caller_type/purpose 组合
- `tests/test_api.py`
  - 补充 recall/search/write 的 API 422 和边界值
- `tests/test_routing_invocation.py`
  - 补充 fixed override + context attach
  - 补充 invoke_from_route 不重复预取
- `tests/test_database_migrations.py`
  - 补充 context_text round trip
  - 补充 memory/knowledge 表结构检查
- 新增 `tests/test_agent_context_security.py`
  - caller_type 伪造
  - source 声明外访问
  - tenant/subject 隔离
- 新增 `tests/test_real_backend_boundaries.py`
  - mem0 缺失 fallback
  - Milvus 缺失错误
  - database backend round trip

## 10. 手工与探索性测试

### 10.1 本地 API 手工检查

- 使用 TestClient 或本地 uvicorn 调用 `/api/v1/knowledge/search`，人工检查 denied source 和 logs。
- 调用 `/api/v1/memories/debug`，检查 memory write、cleanup、event 可见。
- 在本地测试 UI 中确认 debug/admin 状态可展示，但普通 chat UI 不出现自动 memory-write prompt。

### 10.2 配置探索

- 将 knowledge source 全部拒绝，确认 Agent 调用继续。
- 将 memory/knowledge timeout 设置为极小值，确认降级稳定。
- 将 context_per_item_char_limit 设置为 1、8、2000，确认截断行为稳定。
- 将 registry backend 设置为 file、database、hybrid，确认 Agent context 加载一致。

## 11. 准入与退出标准

### 11.1 测试准入

- OpenSpec change 文件完整，`proposal.md`、`design.md`、`tasks.md`、spec 可读。
- 代码能启动或至少能导入 FastAPI app。
- 核心 schema 和 service 已合并到同一分支。
- 测试数据不包含真实用户、电话、机构、token、API key。

### 11.2 测试退出

必须满足：

- P0 自动化用例全部通过。
- `.venv/bin/python -m pytest` 通过。
- `.venv/bin/python -m ruff check .` 通过。
- `.venv/bin/python -m ruff format --check .` 通过。
- `openspec validate add-agent-context-memory-knowledge --strict` 通过。
- API 文档、Agent Definition 文档与实际 schema 对齐。
- 未完成的真实 PostgreSQL、Milvus、mem0 集成必须以风险项记录，不能以 memory fallback 通过代替。

建议满足：

- P1 自动化用例通过率 95% 以上。
- 前端本地测试 UI 涉及 debug/admin 展示时，`cd web && npm run test` 和 `cd web && npm run build` 通过。
- 条件阻塞环境至少有一次冒烟记录。

## 12. 缺陷分级

| 级别 | 定义 | 示例 |
| --- | --- | --- |
| Blocker | 破坏核心治理或主链路不可用 | 未授权 source 出现在 context；route 因 knowledge timeout 失败 |
| Critical | 影响安全、权限、持久化或契约稳定 | 跨租户 memory 被召回；context 字段缺失；database round trip 丢失 context |
| Major | 功能不符合规格但有绕行方案 | `max_items=0` 回落默认；debug log 缺 denied source |
| Minor | 局部体验或可观测不足 | metadata 字段命名不一致；summary 格式不稳定 |
| Trivial | 文档、命名、样例问题 | docs 示例与实际默认值不一致 |

## 13. 已知高风险检查清单

- [ ] `max_items=0` 是否真的代表不取，而不是回落默认。
- [ ] `get_context_repository_bundle()` 在 `storage_backend=database` 下是否仍只用内存仓库。
- [ ] `knowledge_vector_backend=milvus` 是否只是 import 后 fallback，而非真实向量检索。
- [ ] `memory_strategy_provider=mem0` 异常 fallback 是否会掩盖生产配置错误。
- [ ] `/knowledge/search` 是否能被 caller_type/caller_id 伪造越权。
- [ ] Agent 声明外 source 是否能通过受控检索或直接 API 返回。
- [ ] Route preview 与 invoke_from_route 是否重复预取。
- [ ] partial context input 是否造成一侧 context 永远缺失。
- [ ] 固定问 strong denied 是否完全绕过 LLM fallback。
- [ ] route/run/debug log 是否泄露敏感 metadata。

## 14. 最终验收报告模板

```text
变更名称：add-agent-context-memory-knowledge
测试日期：
测试负责人：
代码分支/提交：

结论：
- 通过 / 有条件通过 / 不通过

执行摘要：
- P0：通过 x / 失败 x / 阻塞 x
- P1：通过 x / 失败 x / 阻塞 x
- P2：通过 x / 失败 x / 阻塞 x

已执行命令：
- .venv/bin/python -m pytest
- .venv/bin/python -m ruff check .
- .venv/bin/python -m ruff format --check .
- openspec validate add-agent-context-memory-knowledge --strict
- cd web && npm run test
- cd web && npm run build

关键缺陷：
1. [级别] 标题
   影响：
   复现：
   期望：
   实际：

真实基础设施验证：
- PostgreSQL：
- mem0：
- Milvus：

安全与权限结论：
- Memory 隔离：
- Knowledge source policy：
- caller metadata 伪造：
- debug 脱敏：

遗留风险：
-

验收建议：
-
```
