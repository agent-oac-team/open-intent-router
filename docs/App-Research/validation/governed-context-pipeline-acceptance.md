# Governed Context Pipeline 验收记录

## 范围

本次只改造 OIR 上下文链路，不包含 OAC/IRS 适配、自动记忆写入、知识入库/索引、Artifact Store 或新的用户 Citation 公共字段。

## Rollout 验收

| 模式 | 验收结果 | 模型输入 |
| --- | --- | --- |
| `legacy` | 通过 | 保留旧 Router Prompt，用于紧急回滚 |
| `observe` | 通过 | 构建新 Pack/Projection/Trace，记录 `legacy_input_hash` 与 `projection_hash`；Router LLM 仍只调用一次并使用旧输入 |
| `enforced` | 通过 | Router Prompt 只消费白名单 Projection；Debug、Trace、dropped/raw metadata 不进入模型 |

Agent execution 在三种模式下都保持公开字段兼容，并由统一 Pipeline 生成 `memory_context`、`knowledge_context` 和 invocation input。等价 Router retrieval 可在同请求复用，Agent 阶段重新执行 source/visibility/budget；不同请求、用户和租户不复用。

## 已知差异

1. `enforced` Prompt 结构与旧 Prompt 不同，路由质量需要通过真实流量 observe 数据持续评估。
2. token 使用当前仍以保守字符换算估算；真实 tokenizer 只作为后续校准项。
3. Trace 是 bounded summary 和 hash，首版不支持完整内容级历史重放。
4. route-stage Memory/Knowledge 默认关闭；启用后会增加相应 Provider 延迟，但 timeout/error 默认降级。
5. 可靠 Knowledge 直接回复复用现有 `reply + assistant_message`，不增加公共 Citation 字段。

## 回滚

将 `CONTEXT_PIPELINE_MODE=legacy` 并重启服务。回滚只恢复旧 Router 输入路径，以下安全边界不得关闭或放宽：

- Agent enabled/access policy 和 access-filtered candidate set。
- Memory user/tenant/subject/scope 隔离、TTL 和删除状态。
- Knowledge source enabled/role/group/tenant policy。
- 敏感字段脱敏、Provider timeout/error 降级和 Route Log bounded 输出。

## Legacy 清理条件

旧 Prompt 路径不在本 change 删除。满足以下条件后另开 change：

1. 测试和目标环境连续一个版本周期使用 `enforced`，无高优先级回滚。
2. observe 数据确认关键意图、澄清率、计划生成和固定问行为无不可接受漂移。
3. Route/Invoke 公共兼容测试、全量 pytest、Ruff 和 OpenSpec strict validation 持续通过。
4. 生产排障已能仅依赖 bounded Context Trace、Route Log 和 Provider 指标。

## 自动化门禁

- 后端全量 pytest：`137 passed`，仅有既有 Starlette TestClient deprecation warning。
- Ruff lint：通过。
- Ruff format check：`111 files already formatted`。
- OpenSpec strict validation：`refactor-governed-context-pipeline` valid。
- 核心代码和默认 Prompt 宿主专有概念扫描：无匹配。
