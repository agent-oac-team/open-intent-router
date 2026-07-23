# Memory Runtime Mode 测试环境验收

## 范围与授权

- 日期：2026-07-23
- 环境：OAC/OIR 测试环境
- 用户已分别授权 `observe` 基线、门禁通过后切换 `on`、真实 transport 验收和紧急 `off` 演练。
- 本证据不包含生产变更，也不代表授权归档 OpenSpec。

## Observe 基线

- 清理退役行为变量后，以 `MEMORY_MODE=observe`、live execution 启动。
- Runtime、Capability 和受保护健康检查均正常；Turn/Outbox、Formation queue/dead-letter、Index out-of-sync/dead-letter、deletion pending 和 missing Trace 均为 0。
- 真实五 Turn 样本得到 4 条准确 `ADD/observed` 和 1 条保守 `PENDING/observed`；未创建 Memory Item、Revision 或 provider index。
- 修正测试 Formation model 后，历史 7 条 model dead-letter 原位重试成功，最终 dead-letter 为 0。

## On 与索引收敛

- 经授权切换 `MEMORY_MODE=on`，Runtime 报告 live execution、Recall enabled、Formation enforced、Formation/Index/TTL worker enabled。
- 五 Turn enforced Formation 创建 5 条 Canonical Memory Item、5 条 accepted ADD decision 和 5 条 Revision。
- 旧 Milvus Lite 数据库存在过期 schema，因此保留旧库用于回退，并切换到新的隔离数据库。对 `tenant_id=oac` 执行非 rebuild repair：
  - canonical count：5
  - added count：5
  - failed count：0
  - status：`ok`
- 5 条旧 provider fallback dead-letter 均满足：tenant 为 `oac`、operation 为 `add`、operation revision 等于 current revision、canonical item 为 `ready`。只对这 5 条记录执行 repair reconciliation，原始失败原因保存在 metadata，最终 index dead-letter 为 0。
- 最终健康：Mem0 `ok`，Formation queue/dead-letter、Index out-of-sync/dead-letter、deletion pending 均为 0。

## Formation -> Recall Transport

- 使用 synthetic 测试账号完成真实 OAC 登录，经 OAC Go Server 和 V2 HMAC Route 进入 OIR；Registry dependency 为 `ok`，current/accepted signature 均仅为 V2。
- 初次验收发现 `defer_usage_event=True` 被错误传入 Mem0 metadata filters，导致 Route Recall 稳定返回空候选。修复后增加回归测试，证明 `consumer`、`defer_usage_event`、request/session/turn/run ID 等观测控制键不会进入 provider filters。
- 聚焦验证：`tests/test_mem0_memory_loop.py` 为 `15 passed`；受影响文件 Ruff check 与 format check 均通过。
- 请求 `runtime-memory-on-recall-fixed-1784784783-2`：
  - Route HTTP 200，Canonical Turn 为 `turn_920e797646414553a306fa57f6c869f2`。
  - `route_memory` provider 状态 `ok`，候选数 5。
  - 5 个 Memory Context item 全部 `included`。
  - Admin Memory debug 返回 5 条同 request/session、consumer=`router`、projection outcome=`included` 的 `memory_recall_used` 事件。
- Router/Memory provider 重启后的首次查询可能超过当前 3 秒 prefetch timeout；同进程后续热查询正常。该现象不阻塞本期测试上线，但应作为稳定窗口延迟观察项。

## 紧急 Off 演练

- 演练前保存 `/var/backups/oir-oac-test.env.pre-memory-off-drill-20260723`，只将 `MEMORY_MODE=on` 改为 `off` 并重启。
- Off Runtime 符合固定矩阵：Recall disabled、Formation off、Formation worker/sweeper disabled；Index worker 与 TTL sweeper 继续 enabled。
- exact-preference Route 正常返回，但 Context 不再组装 `route_memory` provider；synthetic active canonical item 数保持 `5 -> 5`。
- 从备份恢复 `MEMORY_MODE=on` 并重启后，第二条恢复请求的 `route_memory` 状态为 `ok`、候选数 5、included 数 5；健康计数继续为 0。

## Replay 与结论

- Route Golden：10/10；Knowledge Golden：17/17。权限放宽、跨用户暴露、核心事实矛盾和重复写 blocking diff 均为 0。
- 已批准的非阻塞差异仅包括 Go 更早拒绝非法 edition，以及无 Agent/Plan/副作用的保守 `reply`/`clarify` 波动。
- Memory 8.5、8.6、8.7 的测试环境门禁完成。保留首次 provider 冷启动超时观察项，暂不归档该 change。
