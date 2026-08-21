# Native Definition v2 硬切与回滚 Runbook

本 Runbook 用于 Native Agent Definition 从旧 `type + invocation + ui_handoff`
形状一次性切换到 `schema_version: oir-agent-v2 + handling`。它是离线维护窗口操作：
不能在仍接受 Native 写入或仍启动旧/新混合 OIR 二进制时执行。OAC Legacy wire 不走此工具，
仍由 OAC Host Adapter 兼容。

## 边界与前置条件

- 先从流量入口停止 Native Admin 写入和新的 Native Direct / Route / Plan 执行；不要把
  `prepare` 当作运行时 feature flag。
- 排空或显式终态化所有引用旧 Definition 的非终态 Plan 与 Run。工具会再读取 canonical
  数据库状态，并拒绝仍有在途记录的切换。
- 运行命令的账户必须能读写 Registry 数据库，或能读写目标 Registry 文件和指定 snapshot
  目录。数据库 rollback material 不经 HTTP/API 暴露；文件 rollback material 以目录 `0700`、
  文件 `0600` 保存。
- `--legacy-runtime-version` 是可恢复的旧二进制版本标识，只接受安全版本标签；不能写入
  连接串、Token 或其他凭据。
- 迁移器不会把旧 HTTP URL、Header、Token、Secret、`provider_config` 或自由 metadata
  搬进 v2；无法表示为安全 logical reference / `SafeHandlingConfiguration` 的条目会在
  dry-run 中以安全错误码拒绝，需先由管理员修正源数据。

以下示例中的 `OIR_MIGRATION_DATABASE_URL` 仅在本机环境中设置，不应回显或写入证据。

## 固定执行顺序

### 1. 记录已冻结的维护窗口

数据库源：

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  prepare \
  --source database \
  --confirm-native-writes-frozen \
  --confirm-new-execution-frozen
```

文件源也必须使用同一 canonical Plan / Run 数据库验证排空：

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  prepare \
  --source file \
  --confirm-native-writes-frozen \
  --confirm-new-execution-frozen
```

`prepare` 只保存可复核的离线操作断言；它不改变 Router 行为，不能替代停止旧进程。

### 2. Dry-run 并审阅安全报告

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  dry-run --source database
```

文件 Registry：

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  dry-run \
  --source file \
  --registry-file ./config/agents.yaml
```

报告只包含数量、逻辑 Agent ID（可安全保留时）、安全错误码和所需 `adapter_key` /
`executor_ref`；不包含源字段、端点、Header、异常文本或凭据。退出码 `0` 表示可继续，
`2` 表示 freeze、drain 或定义验证未满足，必须停止发布。

确认以下条件后才继续：

1. `ready_to_migrate=true`；
2. `active_legacy_plan_count=0` 且 `active_legacy_run_count=0`；
3. 所有 required Runtime Adapter / External Executor capability 已在目标部署清单中确认；
4. `invalid_definition_count=0`。

### 3. 创建 rollback snapshot 并原子迁移

数据库：

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  migrate \
  --source database \
  --legacy-runtime-version oir-legacy-release
```

文件：

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  migrate \
  --source file \
  --registry-file ./config/agents.yaml \
  --snapshot-directory ./private/native-definition-rollback \
  --legacy-runtime-version oir-legacy-release
```

迁移器先持久化私有 snapshot，再对 Database 使用单一事务、对 File Registry 使用同目录
temporary file + atomic replace。提交前会重新读取 input digest、freeze 记录和排空状态；
其中任一变化都以安全错误码拒绝。目标会在提交/替换前按 v2 Schema、Policy 与 Binding
Requirement 再次验证。成功后旧 `type`、`invocation_text`、`ui_handoff_text` 和 metadata
存储值均被清空；此时不能再启动旧程序。

保存命令输出中的 `snapshot_id`（数据库）或 `location`（文件），以及不含敏感值的 dry-run
报告。不要导出或贴出 snapshot 正文。

### 4. 启动新二进制并验证

启动只读 v2 Runtime 后，按发布门禁执行：

1. `/health` liveness 与 `/ready` Runtime / Registry readiness；
2. Public Catalog、Admin Inventory 的脱敏投影；
3. Direct Invoke、Route Invocation、UI Handoff、External Execution、延迟 Plan 与 OAC
   Legacy contract matrix；
4. 全量后端、前端、静态检查与文档 Harness。

新旧 OIR 二进制混合运行不受支持。若任一步失败，停止新二进制并走下一节 rollback。

## Rollback

Rollback 必须先恢复与 snapshot 中 `legacy_runtime_version` 匹配的旧程序，再显式确认：

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  rollback \
  --source database \
  --snapshot-id native_definition_snapshot_example \
  --restore-legacy-binary
```

文件源：

```bash
.venv/bin/python scripts/migrate_native_definitions.py \
  --database-url "$OIR_MIGRATION_DATABASE_URL" \
  rollback \
  --source file \
  --snapshot-file ./private/native-definition-rollback/native_definition_snapshot_example.json \
  --restore-legacy-binary
```

未提供 `--restore-legacy-binary` 时工具会拒绝 rollback，避免把旧数据恢复给仍只读 v2 的
新程序。rollback 还会核对当前 Registry 仍是该 snapshot 对应的 v2 目标；若切换后发生
任何 Registry drift，它会拒绝覆盖，防止新旧数据混合。rollback 结束后重新启动旧程序并验证
旧 Registry、Plan / Run 读取与维护窗口状态；不要在 rollback 后继续运行 v2 Runtime。
