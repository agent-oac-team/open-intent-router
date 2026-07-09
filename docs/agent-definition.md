# Agent 定义

在 `open-intent-router` 中，Agent 是任何可被路由到的能力单元。它可以是真实智能体、HTTP API、后端函数、工作流、前端页面交接点，也可以是用于开发测试的 Mock。

核心设计目标是让路由器只依赖通用 Agent 协议，而不依赖某个具体业务系统、第三方平台或 Agent 产品。

## Agent 类型

MVP 已支持：

- `mock`：返回配置好的响应，用于本地开发、测试和示例。
- `http`：调用外部 HTTP API。
- `local_function`：调用受信任的本地函数。
- `ui_handoff`：返回宿主应用可识别的页面或动作交接信息。

后续可扩展：

- `workflow`：LangChain、LangGraph 或其他工作流引擎。
- `provider_platform`：Coze、Dify、FastGPT 等第三方智能体平台。
- `mcp_tool`：MCP Tool 或外部工具协议。

## 核心字段

Agent Definition 建议包含以下字段：

- `agent_id`：稳定唯一 ID，不应随名称变化。
- `name`：Agent 名称，用于展示和管理。
- `description`：能力描述，也是 LLM 路由时的重要语义依据。
- `type`：Agent 类型，例如 `http`、`local_function`、`mock`、`ui_handoff`。
- `enabled`：是否启用。
- `version`：Agent 定义版本。
- `capabilities`：能力关键词，例如 `summarize`、`search`、`generate`。
- `tags`：管理标签。
- `domain`：领域或业务域，可用于候选收窄。
- `trigger`：触发说明或典型用户表达。
- `access_policy`：访问控制规则。
- `required_inputs`：调用必须具备的输入字段。
- `optional_inputs`：可选输入字段。
- `input_schema`：输入 JSON Schema。
- `output_schema`：输出 JSON Schema。
- `invocation`：后端调用配置。
- `ui_handoff`：宿主应用页面或动作交接配置。
- `context`：声明该 Agent 需要的平台治理上下文，包括记忆和知识检索模式。
- `provider_config`：第三方平台相关配置。
- `metadata`：非核心扩展数据。

## 访问控制

`access_policy` 用于按用户上下文过滤 Agent，常见维度包括：

- `roles`：角色。
- `groups`：用户组。
- `tenants`：租户。
- `attributes`：用户属性匹配。

路由前应先执行可用 Agent 过滤。不可用 Agent 不应进入 LLM 候选集，也不应被 Evidence Provider 的强制路由绕过。

## 调用配置

`invocation` 描述后端如何调用 Agent。不同类型的 Agent 可以使用不同配置，但核心 Schema 不应暴露平台专属字段。

示例原则：

- HTTP Endpoint、Header、Method 等放入 `invocation.config`。
- Coze `bot_id`、Dify App ID 等第三方字段放入 `provider_config` 或 `invocation.config`。
- OAC `route_path` 一类前端路由字段放入 `ui_handoff.route`。
- 飞书表格字段不进入核心 Agent Schema。

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
    source_ids:
      - product_docs
    max_items: 5
```

支持模式：

- `disabled`：不召回。
- `prefetch`：路由/调用目标 Agent 前自动召回，并传入 `memory_context` 或 `knowledge_context`。
- `controlled_retrieval`：固定工作流节点按预设模板调用 `/knowledge/search` 或记忆召回，不允许模型自由决定。

`memory_context` 固定包含 `summary`、`items`、`status` 和截断信息；`knowledge_context` 额外包含 `citations` 和 `source_ids`。低代码 Bot 可以只读取 summary，复杂 Agent 可以读取 items 和 citations。

知识默认不预取，只有 Agent 明确配置 `context.knowledge.mode=prefetch` 才会执行。固定问属于 M6 Evidence Provider 的“问题到意图”映射，不应写入 Agent context，也不作为固定答案能力。

## Registry 后端

当前支持三种注册表模式：

- `database`：从数据库加载 Agent，支持运行时 CRUD。
- `file`：从 YAML / JSON 文件加载 Agent，运行时只读。
- `hybrid`：数据库优先，数据库不可用时使用本地文件兜底。

`hybrid` 模式不会默认合并数据库和文件中的 Agent。如果数据库可用但为空，只有在 `REGISTRY_FILE_FALLBACK_ON_EMPTY=true` 时才会使用文件兜底。

## 扩展原则

新增 Agent 类型时，应优先新增 Invoker 或 Provider Adapter，而不是修改核心路由流程。

建议保持以下边界：

- Agent Definition 只描述能力、访问控制、输入输出和调用方式。
- Router Service 只负责候选筛选、意图识别和路由决策。
- Invocation Service 只负责执行目标 Agent。
- Provider Adapter 只处理第三方平台协议转换。
- 宿主应用专属字段只进入 `metadata` 或专门扩展配置。
