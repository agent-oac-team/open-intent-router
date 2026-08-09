# 迁移实施阈值

## 性能基线

IRS 确定性本地基线各执行 10 次 warmup 和 100 次采样：

| 操作 | p50 | p95 | p99 | 说明 |
| --- | ---: | ---: | ---: | --- |
| Central Route | 1.952 ms | 2.049 ms | 2.166 ms | IRS Stub LLM，关闭 Knowledge Policy |
| Knowledge Search | 0.981 ms | 1.089 ms | 1.180 ms | Fake Embedding/Vector，单 Chunk |
| Knowledge Exact Read | 0.705 ms | 0.739 ms | 0.809 ms | In-memory canonical repository |

本地回归门禁使用 `OIR p95 <= max(IRS p95 * 2, IRS p95 + 5ms)`。该门禁检测 Handler/Schema/Adapter 的明显性能回归，不代表真实 LLM、PostgreSQL 或 Milvus SLA。

## 超时

- Route 总超时：20 秒。超时后提交状态 unknown 时 fail closed，不回退。
- Knowledge Search Provider：3 秒，与 IRS 当前配置默认值一致；按 HTTP 200 + timeout warning 降级。
- Exact Read：2 秒；它只依赖 PostgreSQL canonical store，不等待 Milvus。
- Knowledge Admin 同步上传：120 秒，与 OAC Go Knowledge HTTP Client 当前超时一致。

## Circuit

Circuit 只保护只读操作和能证明 `not_accepted` 的 Route。20 请求滚动窗口、至少 10 个样本，失败率达到 50% 或连续失败 5 次时打开；30 秒后允许最多 3 个 half-open probe，连续成功 3 次关闭，任一 probe 失败重新打开。

Embedding、向量、timeout 和 configuration warning 计入依赖失败；`no_match`、权限过滤、secret 过滤和 disabled 是业务结果，不计入 Circuit 失败。

## Diff

以下差异必须为零：

- blocking Diff、未批准 Route 结构 Diff。
- 权限放宽、跨用户数据、核心事实冲突、重复写入。
- Exact Read 或 Golden Query 必要事实失败。

Search 的预期资产 Recall@5 不低于 0.8。文本措辞和物理 Chunk ID 不要求一致，但原因与行为指纹必须进入报告。

## 稳定窗口

切换门禁要求连续 3 次全量 replay 100% 覆盖，并在 30 分钟内至少完成 1000 次合成操作。期间进程崩溃、unknown commit fallback、写 fallback、Shadow 主数据副作用和未处置 dead-letter 必须全部为零。

这些阈值适用于当前无生产环境、无真实流量的 local/test 迁移，不定义生产 SLA。
