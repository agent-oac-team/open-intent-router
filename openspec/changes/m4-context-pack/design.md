## Context

当前 `ContextService` 会把 host history、agent history、recent results、recent events、Evidence 和 frontend context 放入 `RouteContext.metadata`，然后随路由输入传给 LLM。这个方式适合 MVP，但后续加入 Memory、Knowledge Source、Plan 状态和回放评估后，会出现三个问题：上下文来源没有统一 schema，模型输入没有预算控制，Route Log 无法解释哪些上下文被使用或丢弃。

《中控能力设计文档》把 M4 定义为 M5 Memory 和 M6 Evidence 进入模型的统一前置模块。M4 不负责实现长期记忆或多 Evidence Provider 调度，而是提供 Context Pack、Context Item、Context Budget 和使用摘要，让所有上下文来源都走同一套选择、裁剪和可观测路径。

当前项目必须保持通用开源抽象，不绑定具体业务系统、向量库或模型 tokenizer。M4 首版默认以 token 为预算单位，但在 Provider 没有 token usage 或宿主只提供字符上限时，使用近似字符换算承接。

## Goals / Non-Goals

**Goals:**

- 定义 Context Pack、Context Item、Context Budget 和 Context Usage schema。
- 将当前输入、当前 Agent、Plan、历史消息、最近结果、事件、Evidence 和未来 Memory 统一表示为 Context Item。
- 按优先级、相关性、时效性、权限和预算选择入模型上下文。
- 默认使用 token 预算，支持字符上限与 token 预算换算。
- 在 LLM Provider 或 API 返回 token usage 时优先使用真实 token；未返回时使用近似估算。
- 记录保留、丢弃、压缩和降级原因到 Route Log / Debug。
- 让测试台 Context tab 展示 Context Pack、预算使用和裁剪原因。

**Non-Goals:**

- 不实现长期 User Memory、Memory Store、记忆写入或记忆查看修改 API。
- 不实现多 Evidence Provider 调度、RAG、文件上传解析或向量索引。
- 不实现完整安全脱敏模块；M4 只遵守现有脱敏和权限边界，不扩大敏感数据暴露。
- 不引入模型专用 tokenizer 作为首版必需依赖。
- 不实现场景级预算策略；只预留 schema 或 metadata 承接点。
- 不改变 Agent Registry 的业务权限语义。

## Decisions

### Decision 1: Context Pack 是路由内部主上下文结构

新增 schema 建议包括：

- `ContextPack`
- `ContextItem`
- `ContextBudget`
- `ContextUsage`
- `ContextSelection`

`RouteContext` 继续作为对外响应上下文和兼容载体存在，但 Router / LLM 输入优先使用 Context Pack 的结构化内容。首版以最小 API 扰动为原则，避免破坏现有响应。

M4 首版固定通过 `RouteContext.metadata.context_pack` 暴露 Context Pack 摘要和调试信息，不新增顶层 `RouteResponse.context_pack` 字段。这样可以让测试台 Context tab 读取结构化数据，同时避免在 M4 过早扩大公开 API 面。待 M4/M5/M6 行为稳定后，再评估是否提升为一等响应字段。

Context Item 的基础字段建议包含：

- `item_id`
- `source`
- `scope`
- `role`
- `content`
- `structured_value`
- `priority`
- `relevance`
- `created_at`
- `token_estimate`
- `char_count`
- `included`
- `drop_reason`
- `metadata`

替代方案是继续把上下文放在 `RouteContext.metadata` 的多个数组里。该方案实现成本低，但无法统一裁剪和解释，不适合作为 Memory / Evidence 的共同入口。

### Decision 2: 当前输入和当前任务状态不可被预算裁掉

上下文选择默认优先级：

1. 当前用户输入。
2. 当前场景和当前 Agent 状态。
3. 当前任务 Plan 和未完成步骤。
4. 最近 Agent 结果和用户明确引用的 artifact。
5. 与当前输入相关的 Session / Task / User Memory。
6. 与当前输入相关的 Evidence。
7. 最近会话摘要。
8. 原始历史消息片段。

M4 首版即使尚未实现 Memory，也应保留 Memory 来源类型和空状态，以便 M5 接入。当前输入、当前 Agent、当前 Plan 状态不应因预算不足被裁掉；预算不足时先裁剪低优先级历史、低相关 Evidence 或长结果。

替代方案是简单按时间倒序截断。该方案可能保留最近闲聊却丢掉当前任务状态，影响路由正确性。

### Decision 3: token 是主预算单位，字符换算是兼容路径

配置建议包括：

- 默认总 token 预算。
- Evidence、history、results 等可选分区预算。
- 字符到 token 的近似换算比例。
- 单项最大字符数或 token 数。
- 是否允许压缩摘要。

真实 token usage 的优先级高于估算：如果 LLM Provider 或 API 返回 token 使用量，则记录真实值；如果没有，则使用近似字符换算。模型 tokenizer 估算可作为后续增强，不作为首版默认依赖。

替代方案是引入 tokenizer 库做精确估算。该方案对特定模型更准确，但会增加模型绑定和依赖复杂度，不适合作为通用 MVP 后首版。

### Decision 4: 裁剪先保留可解释性，再追求复杂相关性

首版裁剪规则保持可测试：

- 按 priority、relevance、created_at 和 source policy 排序。
- 超预算时标记 `included=false` 和 `drop_reason`。
- 长文本首版只按单项上限截断，并记录摘要占位信息；不调用额外 LLM 做摘要压缩。
- Evidence、Memory 等未来来源进入模型前必须经过同一预算路径。

替代方案是立即做语义相关性排序或 LLM 摘要压缩。该方案效果可能更好，但难以稳定测试，也会引入额外成本、延迟和失败路径，让 M4 超出基础设施范围。

### Decision 5: Route Log 记录摘要而不是无限原文

Route Log 应记录 Context Pack 使用摘要、预算、保留/丢弃数量、来源分布、裁剪原因和入模型 item id。默认不在日志中复制全部长原文，避免日志膨胀和敏感数据扩散。Debug 或测试台可以展示当前响应中的受控 Context Pack 摘要。

替代方案是把完整 Context Pack 全量写入 Route Log。该方案便于排查，但对隐私、存储和日志可读性都不友好。

### Decision 6: LLM 输入逐步迁移，保持兼容

`LLMRouteInput` 首版通过 `context.metadata.context_pack` 获取结构化内容。Prompt 首版应读取 Context Pack 摘要，同时保留对旧 `RouteContext.metadata` 的兼容，以降低迁移风险。后续如果 Context Pack 成为核心稳定契约，再评估是否在 `LLMRouteInput` 或 `RouteResponse` 中提升为一等字段。

替代方案是一次性删除旧 metadata 上下文。该方案会破坏已有 Mock、Prompt 和外部 LLM 输出测试。

## Risks / Trade-offs

- [Risk] 近似 token 估算不够准确。 -> Mitigation: 配置保守换算比例，Provider 返回 usage 时优先记录真实值，并在日志中标记估算来源。
- [Risk] Context Pack 暴露过多原文。 -> Mitigation: Route Log 默认写摘要和 item id，长文本按单项上限裁剪，继续遵守现有脱敏策略。
- [Risk] 裁剪规则过简单影响路由质量。 -> Mitigation: 首版先保证当前输入和任务状态不丢，再通过 Route Log 和回归样例迭代相关性规则。
- [Risk] 与 M5/M6 的未来需求不完全匹配。 -> Mitigation: schema 保留 Memory、Evidence、Artifact 等通用 source / scope，不提前绑定具体实现。
- [Risk] 迁移 LLM 输入时影响现有 prompt。 -> Mitigation: 采用兼容迁移，Prompt 同时支持 Context Pack 摘要和旧 metadata 结构。

## Migration Plan

1. 新增 Context Pack 相关 schema 和配置项。
2. 扩展 `ContextService`，把当前请求、Agent 状态、历史消息、结果、事件、Evidence 和 Plan 状态转换为 Context Item。
3. 实现预算估算、排序、裁剪和单项截断。
4. 将 Context Pack 接入 Router，并让 LLM 输入使用 Context Pack 摘要或选中项。
5. 将 Context Usage 摘要写入 Route Log 和 Debug metadata。
6. 更新前端 Context tab，展示预算、来源分布、保留项和裁剪原因。
7. 更新 API / 测试台文档。
8. 添加后端和前端测试，运行验证和 OpenSpec 校验。

Rollback 策略：保留旧 `RouteContext.metadata` 上下文生成路径；如 Context Pack 产生异常，可以通过配置或小范围回滚让 LLM 继续读取旧 metadata，同时保留 schema 作为未启用字段。

## Open Questions

None. 场景级预算、Memory 写入、Evidence Provider 调度和独立安全模块都由后续模块分别展开。
