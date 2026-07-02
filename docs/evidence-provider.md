# Evidence Provider

Evidence Provider 是可选插件，运行在 LLM 路由之前，用于向路由器提供外部证据、意图提示或固定问题命中结果。

它的定位不是 Agent Registry，也不是知识库管理系统，而是“给路由决策提供上下文”的轻量扩展点。

## 输出能力

Evidence Provider 可以返回：

- `intent_hint`：弱意图提示，帮助 LLM 理解用户可能想做什么。
- `candidate_agent_ids`：弱候选提示，仅用于上下文和调试；当前版本不用于裁剪 LLM 候选集。
- `route_override`：强固定问题路由，例如固定问法直接映射到某个 Agent。
- `evidence`：可安全记录的证据片段和元数据，用于上下文、调试和审计。

## MVP 插件

当前包含：

- `NoopEvidenceProvider`：空实现，不提供任何证据。
- `FileFixedQuestionEvidenceProvider`：基于本地 YAML 文件的固定问题命中插件。

固定问题配置示例：

```yaml
fixed_questions:
  - question: 帮我打开客户画像页面
    match_type: exact
    strength: strong
    intent_hint: open_customer_profile_help
    candidate_agent_ids:
      - system_usage_guide
    route_override:
      action: open_agent
      target_agent_id: system_usage_guide
      message: 已定位到系统使用指引，将由宿主系统打开客户画像相关页面。
```

## 固定问题命中规则

固定问题适合处理“用户问题与预设问题完全一致或高度稳定”的场景，例如：

- 常见入口问题。
- 高频固定问法。
- 需要稳定映射到特定 Agent 的标准问题。
- 从旧系统知识库迁移来的固定问与意图映射。

强路由覆盖必须在 Registry 可用性过滤之后执行。也就是说，即使固定问题命中了某个 Agent，如果该 Agent 对当前用户不可用，路由器也必须拒绝该覆盖结果，返回无权限提示，并且不进入 LLM 兜底判断。

当前路由顺序为：权限过滤 > 固定问强命中 > 标签/语义观察信号 > LLM 判断。标签/语义信号不会覆盖固定问强命中，也不会裁剪传给 LLM 的候选 Agent。

## 与知识库检索的关系

从 `intent_recon_sys` 迁移而来的知识库检索能力，建议作为后续 Evidence Provider 插件实现，而不是进入核心路由器。

推荐方式：

- 知识库检索插件返回证据片段、来源、分数和候选意图。
- 路由器将检索结果作为 LLM 路由上下文。
- 如果检索结果来自固定问映射，可返回强 `route_override`；强命中目标无权限时应保留 denied 信号，供路由器返回无权限提示。
- 知识库文件管理、向量索引、召回策略等能力留在插件内部。

这样可以让核心项目保持通用，同时保留“固定问命中到固定意图”的扩展能力。

## 安全与审计

Evidence Provider 返回的内容可能进入路由日志，因此应遵守以下约束：

- 不返回未脱敏的密钥、Token 或敏感凭证。
- 证据片段应尽量短，避免写入大段原始文档。
- 元数据中保留来源 ID、命中分数和匹配类型，方便排查。
- 强路由覆盖必须记录命中来源，便于审计为什么绕过了普通 LLM 路由。
