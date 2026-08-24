# Agent 定义

在 `open-intent-router` 中，Agent 是任何可被路由到的能力单元。它可以是真实智能体、HTTP API、后端函数、工作流、前端页面交接点，也可以是用于开发测试的 Mock。

核心设计目标是让路由器只依赖通用 Agent 协议，而不依赖某个具体业务系统、第三方平台或 Agent 产品。

## Native v2 Definition

Native Registry 只有一个 Definition 格式：`schema_version: oir-agent-v2`。旧的 `type`、
`invocation`、`ui_handoff`、`provider_config` 和顶层 `metadata` 都不是 Native Runtime 字段，
不会被 API、文件/数据库 Registry、Router、Plan 或 Invocation 读取或推断。旧 source 只能在
停止运行时通过受控迁移工具处理；OAC 的 Legacy wire 仍只在 Host Adapter 边界转换。

这是一项一次性 Native cutover：混合运行旧/新 OIR Native 二进制，或让旧二进制写入已迁移的
Registry，均不受支持。部署必须先完成受控迁移和发布门禁，再让所有 Native 进程切换到 v2。

核心公共字段包括：

- `agent_id`：稳定、非敏感的逻辑 ID，不应随名称变化。
- `name`、`description`、`capabilities`、`domain`、`tags` 与 `trigger`：展示和路由语义。
- `enabled`、`version`、`revision`：生命周期及并发版本信息。
- `access_policy`：角色、用户组、租户、entitlement 和属性控制。
- `required_inputs`、`optional_inputs`、`input_schema`、`output_schema`：输入输出契约。
- `context`：声明需要的平台治理上下文，包括记忆和知识检索模式。
- `handling`：唯一的执行/协作声明，见下节。

公开 Catalog 和候选投影只公开 `handling_kind`，不会公开 `handling` 的 Adapter、Connector、
Executor 或参数。管理员读取也会脱敏所有 Handling 字符串；管理员编辑后须重新填写被脱敏的值。

## Handling

`handling` 是一个封闭的 discriminated union。它只携带逻辑引用和受限的、部署中立的配置；
endpoint、URL、Header、credential、secret、任意 Provider payload 和宿主私有 metadata 都不属于
Definition。

- `invocation`：`adapter_key`，可选 `connector_ref`，以及 `config` 中的符号操作引用和受限调优。
  Runtime Catalog 在构建 Snapshot 时解析并校验该 Binding；缺失或不兼容时隔离 Definition，而不是
  回退到旧 Invoker 类型。
- `external_execution`：逻辑 `executor_ref` 与受限 `params`。Host External Executor 接受后，
  Runtime 创建受 Ticket 约束的 Delegated Run；Native Runtime 不把它伪装为本地 Invocation。
- `ui_handoff`：内部绝对 `route` 与受限 `params`。它产生 Host 协作动作，不会进入 Invoker。

示例：

```yaml
schema_version: oir-agent-v2
agent_id: script_writer
name: 话术生成
description: 生成客户沟通话术
handling:
  kind: invocation
  adapter_key: copywriter_adapter
  connector_ref: tenant_copywriter
  config:
    function: generate
```

## 访问控制

`access_policy` 用于按用户上下文过滤 Agent，常见维度包括：

- `roles`：角色。
- `groups`：用户组。
- `tenants`：租户。
- `attributes`：用户属性匹配。
- `any_entitlements`：Principal 至少持有其中一项 entitlement，字段内为 OR。

`UserContext.entitlements` 与 `access_policy.any_entitlements` 都是规范化、去重、稳定排序的安全 ASCII 字符串集合，并按完整字符串精确匹配。`any_entitlements` 与其他非空 Policy 维度使用 AND 语义；字段为空表示不增加 entitlement 限制，以保持只使用 role/group/tenant/attribute 的 Host 兼容。deny 条件始终优先。

路由前应先执行可用 Agent 过滤。不可用 Agent 不应进入 LLM 候选集，也不应被 Evidence Provider 的强制路由绕过。
候选为空时 Router 直接返回 `unsupported`，不调用 Agent Evidence 或 LLM。Evidence/LLM target、`continue_agent` 和 Plan 的每个 step 都必须属于同一已授权候选集。

## Context 配置

`context` 是 M5/M6 的产品入口。Agent 只声明需要什么上下文；中控、Invoker 或固定工作流节点负责召回、预算、审计和传参。

```yaml
context:
  memory:
    mode: prefetch
    scopes:
      - user_preference
      - stable_fact
      - task_memory
    max_items: 5
  knowledge:
    mode: disabled
    requirement: optional
    source_ids:
      - product_docs
    max_items: 5
```

支持模式：

- `disabled`：不召回。
- `prefetch`：路由/调用目标 Agent 前自动召回，并传入 `memory_context` 或 `knowledge_context`。
- `controlled_retrieval`：固定工作流节点按预设模板调用 Provider，不允许模型自由决定；
  配置输入兼容短名 `controlled`，保存和输出时统一为 `controlled_retrieval`。

`memory_context` 固定包含 `summary`、`items`、`status` 和截断信息；`knowledge_context` 额外包含 `citations` 和 `source_ids`。低代码 Bot 可以只读取 summary，复杂 Agent 可以读取 items 和 citations。

Knowledge Requirement 支持 `optional|required`，默认 `optional`。`disabled + required`
是非法配置；`prefetch + required` 只有在 Provider 返回至少一条治理后可用 Item 时才允许
调用 Agent。Provider 缺失、拒绝或失败返回 `knowledge_unavailable`，空结果返回
`knowledge_not_found`。optional 路径保留结构化状态并继续调用。

`controlled_retrieval + required` 必须消费 OIR 签发的短时、单次 Knowledge Context
Handle。Handle 绑定 tenant、principal、Agent、Source Scope 与 trace；调用方直接提交的
`knowledge_context`、过期 Handle、跨主体 Handle 和已消费 Handle 均不能作为受信检索证明。

知识默认不预取，只有 Agent 明确配置 `context.knowledge.mode=prefetch` 才会执行。固定问属于 M6 Evidence Provider 的“问题到意图”映射，不应写入 Agent context，也不作为固定答案能力。

## Registry 后端

当前支持三种注册表模式：

- `database`：从数据库加载 Agent，支持运行时 CRUD。
- `file`：从 YAML / JSON 文件加载 Agent，运行时只读。
- `hybrid`：数据库优先，数据库不可用时使用本地文件兜底。

`hybrid` 模式不会默认合并数据库和文件中的 Agent。如果数据库可用但为空，只有在 `REGISTRY_FILE_FALLBACK_ON_EMPTY=true` 时才会使用文件兜底。

## 扩展原则

新增执行能力时，应优先新增受版本约束的 Runtime Adapter、Connector 或 Host Executor，并显式
声明其可接受的 v2 Binding，而不是向 Definition 增加类型字符串或修改核心路由流程。

建议保持以下边界：

- Agent Definition 只描述能力、访问控制、输入输出和声明式 Handling。
- Router Service 只负责候选筛选、意图识别和路由决策。
- Invocation Service 只执行已由 Snapshot 解析的 `invocation` Binding。
- Provider Adapter 只处理第三方平台协议转换。
- 宿主应用专属 wire 字段只留在 Host Adapter；Core 只接收已转换的 v2 Handling。
