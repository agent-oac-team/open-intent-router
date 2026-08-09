# OIR Memory 显式配置与不变性本地基线

## 结论

- 证据日期：2026-07-31（Asia/Shanghai；证据内时间使用 UTC）。
- 环境：本地隔离 OIR，`STORAGE_BACKEND=memory`，显式设置独立的
  `MEMORY_DATABASE_URL`、`MEMORY_MILVUS_*` 和 `MEMORY_EMBEDDING_*`。
- 结果：公开 Memory 写入、代表性召回、删除和删除后召回行为通过；探针执行前后
  active item 均为 0。
- 数据动作：未迁移 SQL 数据，未复制或重建 Milvus collection，未重新 embedding。
- 内容边界：机器证据不保存 Memory 正文、召回 query、租户 ID 或用户 ID；这些值仅以
  SHA-256 摘要出现。

机器证据：
[oir-memory-invariance-local.json](../evidence/migration/oir-memory-invariance-local.json)。

## 本地结果

| 检查项 | 结果 |
| --- | --- |
| 配置来源 | SQL、Milvus URI/collection、embedding model/dims 均从显式 `MEMORY_*` 读取 |
| Collection | `oir_memory_vectors`，未改名 |
| 初始 / 结束 active item 数 | `0 / 0` |
| 代表性召回 | API 正常返回；隔离用户初始无数据 |
| CRUD 探针 | write `accepted`；recall 命中；delete `pending`；删除后不再命中 |
| 正文保存 | `memory_body_included=false` |
| 兼容回退 | 本机仍由 `EMBEDDING_API_KEY` 提供 key，报告将其列为 deprecated fallback |

`delete_status=pending` 表示公开删除 API 已接受硬删除工作；本次 repository-only
隔离运行中，canonical item 已不可召回且结束计数回到 0。真实 mem0 环境仍须验证
provider vector 删除完成状态。

## 复现

先使用目标环境的正常部署方式启动 OIR，再执行：

```bash
.venv/bin/python scripts/capture_memory_invariance.py \
  --base-url http://127.0.0.1:8000 \
  --tenant-id <isolated-tenant> \
  --user-id <isolated-user> \
  --recall-query "<representative-query>" \
  --exercise-crud \
  --output <environment-specific-output.json>
```

默认不加 `--exercise-crud` 时工具只读。启用 CRUD 时，工具创建一个唯一探针并通过
公开 API 删除；应使用专门的隔离 tenant/user。若环境启用 Memory identity 签名，
通过 `--identity-secret-env` 指定包含 secret 的环境变量名；secret 不会进入报告。

## 尚未证明

此证据只证明本地隔离环境，不替代测试环境或生产环境验证。在移除兼容回退前，必须在
对应环境分别采集变更前、显式 `MEMORY_*` 生效后两份报告，并人工比较：

1. effective collection、embedding model/dims 与配置来源；
2. active item count（注意工具上限与 `count_truncated`）；
3. 固定代表性 query 的召回状态、数量和 memory ID；
4. 隔离探针的 write/recall/delete/post-delete 行为；
5. provider vector 删除完成状态及运行日志中无意外 deprecated fallback。

报告的 `comparison_fingerprint` 只覆盖可比较行为字段，不覆盖时间和主体哈希；它可用于
发现配置或行为变化，但不能单独证明召回质量等价。

## 第二阶段收缩验证

删除配置回退后，自动化测试会直接读取上述第一阶段机器证据，并用其中的
`MEMORY_DATABASE_URL`、Collection、Embedding model/dims 构造当前 Settings，确认有效
Collection 和 Embedding 标识未改变；同时复核基线中的前后数量、Recall、Create/Delete
结果和无正文边界。配置测试另行证明 Knowledge、通用 Embedding 与 Router LLM 配置均
不能改变 mem0 配置，缺少必要 `MEMORY_*` 时会列出缺项并失败。

这一验证不执行建库、重建 Collection、re-embedding、数据迁移或批量改写。第一阶段
JSON 保持原始快照，不因第二阶段删除 `deprecated_fallbacks` 响应字段而改写。
