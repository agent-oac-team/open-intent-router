# OIR Knowledge 副本清理本地证据

- 日期：2026-07-31
- 范围：仅本地 `data/oir-migration.db`、`data/test-open-intent-router.db` 与
  `.data/oir_knowledge_milvus.db`
- 结论：本地 OIR Knowledge 副本已清理，Memory 表与
  `.data/oir_memory_milvus.db` 的前后哈希一致；未连接或修改生产环境，也未向
  `knowledge_sys` 迁移数据。

## 执行结果

执行前的无正文 Manifest 记录了固定 9 张 Knowledge 表的行数、Schema
指纹和聚合哈希，以及唯一集合 `oir_knowledge_vectors` 的聚合哈希。
`oir-migration.db` 中 `knowledge_asset_chunks` 为 269 条；
`knowledge_sys` 的已知基线为 270 条，差异按“`knowledge_sys` 是 canonical，
不迁移 OIR 副本”处置。测试库的 0 条记录作为该本地测试库的显式预期值处理。

确认执行只删除固定 9 张 Knowledge 表、`oir_knowledge_vectors` 和本地
`oir_knowledge_milvus.db`。两个数据库均不再包含名称含 `knowledge` 的表；
Memory 表和 Memory 向量路径保留。详细输入指纹、动作和不变性哈希见索引中的
四份 JSON 报告。

本次本地删除的表数据和向量目录未制作恢复副本，不能从 OIR 本地副本恢复；
`knowledge_sys` canonical 数据未受影响。

## 执行后工具加固

执行报告是当次成功执行生成的不可变快照；随后根据代码审阅继续加固工具，
没有重复运行本地清理：

1. 对所有非 Knowledge 表的 `TEXT` 列扫描 `knowledge_context` 和已知可疑正文
   键。命中未列入审阅集合的 `table.column` 时，在任何更新、Drop 或文件操作前
   fail closed。
2. 确认流程采用可恢复两阶段：数据库事务内完成历史净化、固定表 Drop 和
   Memory 校验；随后将 Knowledge 向量目录原子 rename 到同级唯一 quarantine，
   再提交事务。
3. quarantine rename 或事务提交前失败时，回滚数据库并恢复原目录；提交后清理
   quarantine 失败时，返回
   `executed_with_quarantine_cleanup_pending` 和可恢复目录名，不报告向量已删除。

## 验证

- `tests/test_cleanup_oir_knowledge.py` 覆盖无正文 dry-run、未知表/集合/JSON
  结构、未知 `TEXT` 正文位置、无法隔离向量时数据库与历史内容回滚、提交后
  quarantine 删除失败的可恢复报告，以及 Memory 不变性。
- 聚焦回归：25 passed。
- 全量回归：1107 passed, 1 skipped。
- `ruff check .`、`ruff format --check .` 通过。
- `openspec validate replace-irs-with-oir-oac-adapter --strict` 通过。
