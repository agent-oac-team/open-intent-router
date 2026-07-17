# OAC Agent Registry 迁移与主写规则

## 事实源

切换后 OIR PostgreSQL `agent_definitions` 是 Agent Registry 唯一可写事实源。OAC Host 在启用 `OAC_HOST_ENFORCE_REGISTRY_SINGLE_WRITER=true` 时要求：

- Core `REGISTRY_BACKEND=database`；
- 禁止 database 空表时回退 file；
- 禁止 IRS Registry 写入、飞书同步和 file restore；
- 任一冲突配置都会阻止 Host Runtime 启动。

IRS 的 `/api/v1/admin/agent-registry/sync-feishu` 固定返回 `410 feishu_registry_sync_disabled`。IRS Registry CRUD 不再读取或写入飞书。

## Legacy API

OAC Host Adapter 兼容以下 IRS 路径：

- `GET /api/v1/admin/agent-registry`
- `POST /api/v1/admin/agent-registry`
- `PUT /api/v1/admin/agent-registry/{agent_id}`
- `PATCH /api/v1/admin/agent-registry/{agent_id}/enabled`
- `DELETE /api/v1/admin/agent-registry/{agent_id}`

所有写操作要求 `oac_admin` 受信身份。Coze 和普通 OAC 用户不能写 Registry。

## 字段映射

| IRS/OAC 字段 | OIR 通用字段 |
| --- | --- |
| `agent_id/name/description/enabled` | 同名通用字段 |
| `bot_id` | `invocation.provider_config.bot_id` |
| `route_path` | `ui_handoff.route` |
| `allowed_user_tags` | `access_policy.allow_groups` |
| `positive_keywords` | `trigger.keywords/positive_examples` |
| `negative_keywords` | `trigger.negative_examples` |

空 `route_path` 对 Provider Agent 合法；非空路径必须是 OAC 内部绝对路径，禁止外部 URL、`//`、反斜杠和 `..`。

## 版本与审计

每个 Agent 持有单调递增 `revision`。更新、启停和删除以读取到的当前 revision 作为乐观条件；并发旧版本返回 `409 agent_revision_conflict`，不得覆盖新值。

`registry_revisions` 记录 revision、操作、操作者、来源、时间以及脱敏前后快照。公开 Legacy 响应只投影 `bot_id`，不暴露 provider token、secret 或其他私有配置。

## 首次导入

导入和对账入口：

```bash
REGISTRY_BACKEND=database .venv/bin/python scripts/import_irs_agent_registry.py \
  --source /Users/lijingtong/project/intent_recon_sys/sql/agent_registry.csv \
  --report docs/migration/agent-registry-reconciliation.json
```

脚本按稳定 `agent_id` 幂等创建或更新 9 个 Agent。验收报告逐条检查名称、启用状态、权限、正/负触发、调用方式和 UI handoff，不能只比较数量。
