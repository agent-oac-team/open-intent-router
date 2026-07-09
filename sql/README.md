# SQL 初始化说明

本目录保存面向本地开发和联调的 PostgreSQL 建表快照。

- 权威数据模型仍是 `app/db/models.py`。
- 应用启动和 smoke test 仍可通过 `app.db.session.create_all_tables()` 自动建表。
- 当 SQLAlchemy model 新增或修改表结构时，需要同步更新 `postgresql_schema.sql`。
- 本文件只保存表结构和索引，不保存真实密钥、业务数据或测试用户数据。

本地初始化示例：

```bash
psql postgresql://oac:oac@127.0.0.1:5432/oac -f sql/postgresql_schema.sql
```

mem0 记忆闭环相关表：

- `memory_items`：OIR accepted memory 的治理事实。
- `memory_events`：mem0 add/search/delete、外部 ID 映射、失败和 policy 事件 ledger。

知识迁移相关表：

- `knowledge_sources`
- `knowledge_chunks`
- `knowledge_retrieval_logs`

Milvus collection 不在 PostgreSQL 中建表；`oir_memory_vectors`、`oac_knowledge_chunks`、`oir_knowledge_vectors` 是向量索引 collection，迁移时以 PostgreSQL canonical metadata/chunks 为准重新索引。
