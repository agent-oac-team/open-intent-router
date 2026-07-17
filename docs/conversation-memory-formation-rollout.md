# Conversation Memory Formation 上线与回退

本文定义自动形成闭环的配置、部署顺序、质量门槛和紧急回退。PostgreSQL 是 canonical store；mem0ai 2.0.11 + Milvus Lite 仅是可从 active projection 重建的派生索引。本文不引入新的 Router intent。

## 配置边界

配置项以 `.env.example` 为准，分为六组：

- 形成策略：`MEMORY_FORMATION_MODE`、5-turn/30-second、model/prompt/policy version、0.90/0.70 thresholds。
- 恢复语义：model timeout、lease、max attempts、指数 retry 范围。
- 后台进程：formation worker/sweeper、index worker、TTL sweeper、maintenance/sweep interval、consolidation。
- 内容上限：user/assistant/summary capsule chars 和 prompt max chars。
- 独立关闭：`MEMORY_FORMATION_MODE` 控制自动形成，`MEMORY_RECALL_ENABLED` 控制召回，
  `MEMORY_FORMATION_WORKER_ENABLED` 控制形成 Job Worker；三者互不替代。
- 隔离执行：`MEMORY_EXECUTION_MODE=decision_shadow` 禁止 Memory 召回和写副作用；
  `state_rehearsal` 必须配置独立 `MEMORY_REHEARSAL_DATABASE_URL` 和
  `MEMORY_REHEARSAL_MILVUS_COLLECTION`，且不得指向主数据域。

自动 Formation 的唯一可信入口是已完成 Canonical Turn 的 `turn.completed` Transactional
Outbox。Consumer 重新读取 Turn 并验证 tenant/user/session/request 所有权、终态、最终语义响应和
Result 引用后才构造 Capsule。Plan、Run、Result、Event 或 Host 消息不能脱离 completed Turn 自动
形成记忆。重复 Outbox 投递按 canonical `turn_id`、policy version 和冻结 window 收敛。

`GET /api/v1/runtime/config` 只暴露非敏感模式、版本、worker 开关、queue/dead-letter/index/delete health。管理员使用 `GET /api/v1/admin/memories/health` 和 `GET /api/v1/admin/memories/metrics` 查看完整无正文聚合；响应不得包含 API key、token、数据库密码、prompt 或候选原文。

生产的 `/route*`、`/invoke` 和 `/plans/*` 必须由受信网关注入 `X-User-ID`、`X-Tenant-ID` 和基于 `MEMORY_IDENTITY_SECRET` 的 `X-Memory-Identity-Signature`。body/query ownership 只作业务输入并由签名 identity 覆盖，不是认证来源。

## 上线顺序

### 1. Schema 与 mode off

1. 部署 PostgreSQL schema 和服务，保持 `MEMORY_FORMATION_MODE=off`、formation worker/sweeper 关闭。
2. 保持 `MEMORY_INDEX_WORKER_ENABLED=true`、`MEMORY_TTL_SWEEPER_ENABLED=true`，让显式写入、删除和已有 maintenance 正常工作。
3. 验证 legacy active memory 的 deterministic key/revision-1 backfill 可重跑，Plan ownership schema 拒绝无 owner 数据。
4. 验证 `/runtime/config` 无凭证、formation queue/dead-letter 为零，显式 write-candidates 和既有 `memory_context` 无回归。

### 2. Observe

1. 设置 `MEMORY_FORMATION_MODE=observe`，开启 formation worker/sweeper；consolidation 初始关闭。
2. 至少覆盖完整 5-turn、30-second idle、无 session close、重复 Turn Outbox 和 temporary/private 流量。
3. 抽样审查 evidence、scope、subject、memory key、ADD/UPDATE/DELETE/NOOP/REJECT/PENDING reason；observe 不允许 current/revision/provider side effect。
4. 连续观察一个有代表性的流量周期，并用下方 gate 决定是否进入 enforced。

### 3. 隔离 Enforced

1. 为单独服务实例或流量分区配置 `MEMORY_FORMATION_MODE=enforced`，只接收一个明确的 tenant allowlist；当前配置是实例级开关，隔离必须在流量入口完成，不能假定应用内存在 tenant feature flag。
2. 先启用 preference 和 structured task 场景，保持 consolidation 关闭。
3. 验证 canonical commit 先于 provider operation、stable-ID UPDATE、deletion pending fail-closed、repair/rebuild 和 owner-scoped Plan continuation。
4. gate 连续通过后再扩大 tenant 范围；任何阶段不通过都回到 off 或 observe，不回滚 canonical schema。

## Rollout Gates

以下是首版默认门槛；应在真实 observe 基线稳定后按业务量调整，并记录调整原因。

| 类别 | 放行门槛 |
| --- | --- |
| Queue | queue depth 无持续增长；oldest pending 小于 120 秒，且小于 worker 可恢复 SLA |
| Dead letter | formation、index、delete dead-letter 均为 0；出现任意一条即停止扩量 |
| Index | steady-state `index_out_of_sync_count=0`；repair 后不留下 duplicate/orphan vector |
| Delete | deletion pending 最老年龄小于 300 秒；抽样确认 current/revisions/provider data 均删除且 tombstone 无正文 |
| Precision | 人工抽样 accepted ADD/UPDATE precision 至少 95%；stable fact 和 DELETE 必须 100% 有权且证据充分 |
| Correction | enforced 后 7 天内用户纠正/撤销自动 memory 的比例不超过 accepted decision 的 2% |
| NOOP | 与 observe 基线偏差不超过 20 个百分点；NOOP 超过 80% 或突增 2 倍时暂停扩量并排查重复触发/模型漂移 |
| Pending/Reject | 不设越低越好的错误目标；按 scope/reason 与 observe 基线比较，异常突变必须解释 |
| Cost | 每 job model usage/cost 不超过批准预算；usage 字段必须为 allowlist 数值且不含正文 label |
| Latency | 当前 metrics 用 `*_latency_ms_total / job_count` 计算平均 model/job latency，分别小于 10/15 秒且低于 20 秒 timeout；主响应 p95 相对 off 增量小于 50 ms 由外部 APM 计算 |
| Isolation | 跨 tenant/user/subject 的 recall、formation、debug、management、consolidation 命中为 0 |

质量比例的分母、抽样窗口和样本量必须随发布记录，不得只展示百分比。DELETE、DLP 和 ownership 违规属于零容忍事件，不由总体 precision 稀释。

### 可执行采集

```bash
curl -fsS -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  http://127.0.0.1:8000/api/v1/admin/memories/health > memory-health.json
curl -fsS -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  http://127.0.0.1:8000/api/v1/admin/memories/metrics > memory-metrics.json

jq '{queue_depth, oldest_pending_seconds, dead_letter_count,
     index_out_of_sync_count, index_dead_letter_count, deletion_pending_count}' \
  memory-health.json
jq '{jobs: ([.formation_series[].job_count] | add // 0),
     decision_rates, model_usage_total,
     avg_model_latency_ms:
       (if ([.formation_series[].job_count] | add // 0) == 0 then 0
        else .model_latency_ms_total / ([.formation_series[].job_count] | add) end),
     avg_job_latency_ms:
       (if ([.formation_series[].job_count] | add // 0) == 0 then 0
        else .job_latency_ms_total / ([.formation_series[].job_count] | add) end),
     deletion_oldest_pending_seconds}' memory-metrics.json
```

precision review 表至少包含 `job_id/scope/operation/reviewer/correct`；`precision = correct / reviewed accepted`。correction 表至少包含被用户 confirm/reject/delete 的自动 memory ID 和原 formation job；`correction rate = corrected accepted / accepted`。cost 使用 `model_usage_total` 的 allowlisted token 数乘经批准单价，代码和指标不得内置供应商价格或正文 label。NOOP 使用 `decision_rates.noop` 与同口径 observe 基线比较。

## Emergency Mode-Off Drill

1. 将 `MEMORY_FORMATION_MODE=off`、`MEMORY_FORMATION_WORKER_ENABLED=false`、`MEMORY_FORMATION_SWEEPER_ENABLED=false`、`MEMORY_CONSOLIDATION_ENABLED=false` 后滚动重启。
2. 保持 index worker 和 TTL sweeper 开启，以完成已接受的 index/delete/TTL maintenance；off 不是撤销已提交 canonical operation。
3. 发起一个完整 Turn 并消费其 Outbox，确认记录 `formation_mode_off` 跳过原因，且没有新的
   formation turn/job/decision side effect。
4. 验证显式 write-candidates、recall、用户删除、operation status、repair 和 runtime/debug 查询仍工作。
5. 观察 queue/dead-letter/index/delete health，保存切换时间、最后处理 job、未完成 operation 和恢复决策。

回退不删除 PostgreSQL schema、revision 或 tombstone，也不从 mem0 history恢复 canonical 内容。
重新开启时 worker 依靠 lease、idempotency key、watermark 和 outbox 恢复，不人工复制 provider
vector。禁止设置 `MEMORY_IMPORT_LEGACY_HISTORY_ENABLED=true`；OIR 不从 IRS/OAC 历史消息、
Session、Plan、Result 或 Event 初始化 Memory。

## 验收命令与 2026-07-14 证据

```bash
.venv/bin/pytest -q tests/test_memory_acceptance_matrix.py \
  tests/test_memory_end_to_end_acceptance.py \
  tests/test_memory_trigger_runtime.py \
  tests/test_memory_lifecycle_service.py \
  tests/test_memory_invocation_plan_integration.py \
  tests/test_plan_repository_ownership.py
.venv/bin/pytest -q tests/test_memory_postgresql_integration.py
.venv/bin/python scripts/smoke_mem0_memory_loop.py
```

本次是本地预发布机制演练，不是生产扩量批准。记录如下：

| 阶段 | 隔离范围与样本 | 结果 |
| --- | --- | --- |
| off | 1 个完整 invocation + 1 个 structured event | 自动 turn/job/decision 为 0；显式 recall/index/TTL maintenance 测试通过 |
| observe | 1 个 idle job、1 个 five-turn job；deterministic processor 提议 1 个 ADD | worker 执行 `execute_lifecycle=false`，current/revision/provider side effect 为 0 |
| PostgreSQL race | 随机 `pg_acceptance_tenant_*`；5 turns、2 job workers、2 revision writers、2 outbox workers | 1 个 executable job；revision 仅 1 个 CAS 胜者且链为 1→2；两个 outbox worker 领取不同 operation；测试后 tenant 行清理 |
| isolated enforced | 随机 `smoke_tenant_*`；preference ADD/UPDATE/delete + 1 running Plan | 人工审查 accepted 3/3 正确，correction 0/3，NOOP 0/3；current/revisions/provider 硬删除、单 vector、rebuild、owned continuation 通过 |
| emergency off | 1 个 formation-off runtime；index 与 TTL maintenance 各有待处理工作 | formation worker/sweeper 停止且 maintenance 继续消费；队列无 dead-letter/out-of-sync 残留 |

deterministic observe/enforced 样本没有外部 formation model 调用，因此 model token/cost 为 0，不能作为生产成本基线；3 个 accepted 的人工样本也不足以满足 95% production precision gate。正式扩量前仍必须按“可执行采集”连续记录真实 observe 窗口、样本分母、平均 latency、APM p95、correction、NOOP 和成本。该限制不影响代码级 off/observe/enforced/rollback 机制验收。

真实 PostgreSQL + mem0ai 2.0.11 + Milvus Lite smoke 以 `SMOKE_OK` 为通过标志。PostHog、gRPC fork 和可选 spaCy extra 提示属于非阻塞环境噪音，不能替代数据库/provider 断言。

## 三层策略上线观测

扩量前按 `semantic_contract_version` 统计 validation confirmed/pending 与 verifier confirmed/contradicted/uncertain；这些 label 不得包含 slot value、quote、prompt 或正文。临时回答语言安全过滤器命中率单独统计，命中只能减少自动写入，不能提升 accepted rate。若 Assistant-only durable candidate、semantic/structured value 冲突却被 accepted，或 verifier confirmed 未重新执行 current-state/hard-rule 检查，立即切回 observe/off 并阻止扩量。
